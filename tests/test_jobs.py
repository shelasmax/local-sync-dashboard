from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database import migrate_sync_jobs_table
from app.models import ProfileType, StorageProfile, SyncJob
from app.schemas import SyncJobPayload
from app.services.common import encode_json
from unittest.mock import patch

from app.models import RunHistory, RunStatus
from app.services.jobs import (
    JobConflictError,
    JobDeleteError,
    JobRunner,
    RunDeleteError,
    RunRecoveryError,
    RunSnapshot,
    cleanup_runs,
    delete_run,
    delete_job,
    preview_job,
    retry_run,
    validate_job_route,
    validate_job_endpoints,
)
from app.services.rclone import RcloneResult
from app.services.rclone import build_copy_command


class DummyRunner:
    def __init__(self) -> None:
        self.active_runs: dict[int, object] = {}
        self.synced: list[int] = []

    def sync_job(self, job_id: int) -> None:
        self.synced.append(job_id)


class JobsServiceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.source_root = f"{self.tempdir.name}/source"
        self.target_root = f"{self.tempdir.name}/target"
        os.makedirs(self.source_root, exist_ok=True)
        os.makedirs(self.target_root, exist_ok=True)
        self.engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.session = TestingSession()
        self.runner = DummyRunner()

        source = StorageProfile(
            name="Source",
            profile_type=ProfileType.LOCAL_FOLDER.value,
            root_path_or_remote=self.source_root,
            status="ready",
            options_json=encode_json({}),
        )
        target = StorageProfile(
            name="Target",
            profile_type=ProfileType.YANDEX_REMOTE.value,
            root_path_or_remote="yadisk:",
            status="ready",
            options_json=encode_json({}),
        )
        self.session.add_all([source, target])
        self.session.commit()
        self.session.refresh(source)
        self.session.refresh(target)
        self.source = source
        self.target = target

    def tearDown(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.tempdir.cleanup()

    def _create_job(self) -> SyncJob:
        job = SyncJob(
            name="Source -> Target",
            source_profile_id=self.source.id,
            target_profile_id=self.target.id,
            source_path="",
            target_path="",
            schedule="manual",
            enabled=True,
            mode="copy",
            filters_json="[]",
            verify_checksum=False,
        )
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def test_delete_job_removes_inactive_job(self):
        job = self._create_job()

        delete_job(self.session, job.id, self.runner)

        self.assertIsNone(self.session.get(SyncJob, job.id))
        self.assertEqual(self.runner.synced, [job.id])

    def test_delete_job_rejects_active_job(self):
        job = self._create_job()
        self.runner.active_runs[job.id] = RunSnapshot(job_id=job.id)

        with self.assertRaises(JobDeleteError):
            delete_job(self.session, job.id, self.runner)

        self.assertIsNotNone(self.session.get(SyncJob, job.id))

    def test_delete_run_removes_log_for_finished_run(self):
        job = self._create_job()
        log_path = Path(self.tempdir.name) / "run.log"
        log_path.write_text("run log", encoding="utf-8")
        run = RunHistory(
            job_id=job.id,
            status=RunStatus.SUCCESS.value,
            summary="done",
            log_path=str(log_path),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        delete_run(self.session, run.id, self.runner)

        self.assertIsNone(self.session.get(RunHistory, run.id))
        self.assertFalse(log_path.exists())

    def test_delete_run_rejects_running_run(self):
        job = self._create_job()
        run = RunHistory(
            job_id=job.id,
            status=RunStatus.RUNNING.value,
            summary="running",
            started_at=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        with self.assertRaises(RunDeleteError):
            delete_run(self.session, run.id, self.runner)

    def test_cleanup_runs_filters_by_status_and_age(self):
        job = self._create_job()
        old_success_log = Path(self.tempdir.name) / "old-success.log"
        old_success_log.write_text("ok", encoding="utf-8")
        old_success = RunHistory(
            job_id=job.id,
            status=RunStatus.SUCCESS.value,
            summary="old success",
            log_path=str(old_success_log),
            started_at=datetime.now(UTC) - timedelta(days=40),
            finished_at=datetime.now(UTC) - timedelta(days=39),
        )
        recent_success = RunHistory(
            job_id=job.id,
            status=RunStatus.SUCCESS.value,
            summary="recent success",
            started_at=datetime.now(UTC) - timedelta(days=2),
            finished_at=datetime.now(UTC) - timedelta(days=2),
        )
        old_failed = RunHistory(
            job_id=job.id,
            status=RunStatus.FAILED.value,
            summary="old failed",
            started_at=datetime.now(UTC) - timedelta(days=40),
            finished_at=datetime.now(UTC) - timedelta(days=39),
        )
        self.session.add_all([old_success, recent_success, old_failed])
        self.session.commit()

        result = cleanup_runs(
            self.session,
            self.runner,
            status=RunStatus.SUCCESS.value,
            older_than_days=30,
        )

        self.assertEqual(result.deleted_runs, 1)
        self.assertEqual(result.deleted_logs, 1)
        remaining_statuses = {
            run.summary: run.status
            for run in self.session.query(RunHistory).filter(RunHistory.job_id == job.id).all()
        }
        self.assertIn("recent success", remaining_statuses)
        self.assertIn("old failed", remaining_statuses)
        self.assertNotIn("old success", remaining_statuses)
        self.assertFalse(old_success_log.exists())

    def test_create_job_rejects_duplicate_name(self):
        self._create_job()

        duplicate = SyncJobPayload(
            name="Source -> Target",
            source_profile_id=self.source.id,
            target_profile_id=self.target.id,
            source_path="nested",
            target_path="nested",
            schedule="manual",
            enabled=True,
            mode="copy",
            filters=[],
            verify_checksum=False,
        )

        with self.assertRaises(JobConflictError):
            from app.services.jobs import create_job
            create_job(self.session, duplicate)

    def test_build_copy_command_supports_advanced_runtime_flags(self):
        command = build_copy_command(
            source="/tmp/source",
            target="yadisk:target",
            transfers=8,
            checkers=16,
            fast_list=True,
        )

        self.assertIn("--transfers", command)
        self.assertIn("8", command)
        self.assertIn("--checkers", command)
        self.assertIn("16", command)
        self.assertIn("--fast-list", command)

    def test_create_job_persists_advanced_rclone_flags(self):
        from app.services.jobs import create_job

        payload = SyncJobPayload(
            name="Advanced copy",
            source_profile_id=self.source.id,
            target_profile_id=self.target.id,
            source_path="",
            target_path="",
            schedule="manual",
            enabled=True,
            mode="copy",
            filters=[],
            verify_checksum=False,
            rclone_transfers=8,
            rclone_checkers=16,
            rclone_fast_list=True,
        )

        job = create_job(self.session, payload)

        self.assertEqual(job.rclone_transfers, 8)
        self.assertEqual(job.rclone_checkers, 16)
        self.assertTrue(job.rclone_fast_list)

    def test_migrate_sync_jobs_table_adds_new_columns_for_existing_db(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE sync_jobs (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120),
                    source_profile_id INTEGER,
                    source_path VARCHAR(512),
                    target_profile_id INTEGER,
                    target_path VARCHAR(512),
                    schedule VARCHAR(120),
                    enabled BOOLEAN,
                    mode VARCHAR(16),
                    filters_json TEXT,
                    bandwidth_limit VARCHAR(32),
                    verify_checksum BOOLEAN,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )

        migrate_sync_jobs_table(self.engine)

        with self.engine.begin() as connection:
            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(sync_jobs)").fetchall()
            }

        self.assertIn("rclone_transfers", columns)
        self.assertIn("rclone_checkers", columns)
        self.assertIn("rclone_fast_list", columns)

    def test_preview_job_builds_dry_run_summary(self):
        job = self._create_job()

        with (
            patch("app.services.jobs.estimate_size") as estimate_size_mock,
            patch("app.services.jobs.execute") as execute_mock,
        ):
            estimate_size_mock.return_value.bytes_total = 4096
            estimate_size_mock.return_value.files_total = 12
            estimate_size_mock.return_value.command_preview = "rclone size /tmp/source --json"

            execute_mock.return_value.command_preview = "rclone copy /tmp/source yadisk: --dry-run"
            execute_mock.return_value.stdout = "dry-run output"
            execute_mock.return_value.stderr = ""
            execute_mock.return_value.returncode = 0
            execute_mock.return_value.bytes_transferred = 0
            execute_mock.return_value.files_transferred = 3
            execute_mock.return_value.canceled = False

            preview = preview_job(self.session, job.id)

        self.assertEqual(preview.job_id, job.id)
        self.assertEqual(preview.source_files, 12)
        self.assertEqual(preview.source_bytes, 4096)
        self.assertIn("--dry-run", preview.command_preview)

    def test_preview_job_rejects_missing_local_source_before_rclone(self):
        job = self._create_job()
        self.source.root_path_or_remote = "/definitely-missing-source"
        self.session.commit()

        with (
            patch("app.services.jobs.estimate_size") as estimate_size_mock,
            patch("app.services.jobs.execute") as execute_mock,
        ):
            with self.assertRaisesRegex(Exception, "Локальный источник недоступен"):
                preview_job(self.session, job.id)

        estimate_size_mock.assert_not_called()
        execute_mock.assert_not_called()

    def test_validate_job_endpoints_rejects_missing_local_target_root(self):
        local_target = StorageProfile(
            name="Local Target",
            profile_type=ProfileType.LOCAL_FOLDER.value,
            root_path_or_remote="/definitely-missing-target",
            status="ready",
            options_json=encode_json({}),
        )
        self.session.add(local_target)
        self.session.commit()
        self.session.refresh(local_target)

        with self.assertRaisesRegex(Exception, "Локальное назначение недоступно"):
            validate_job_endpoints(self.source, local_target)

    def test_validate_job_route_rejects_duplicated_synology_segment(self):
        source = StorageProfile(
            name="Synology",
            profile_type=ProfileType.SYNOLOGY_SHARE.value,
            root_path_or_remote="/Volumes/home/Ext_HDD",
            status="ready",
            options_json=encode_json({}),
        )

        with self.assertRaisesRegex(Exception, "дублирует последнюю папку"):
            validate_job_route(source, self.target, source_path="Ext_HDD")

    def test_validate_job_route_rejects_synology_path_outside_volumes(self):
        source = StorageProfile(
            name="Synology",
            profile_type=ProfileType.SYNOLOGY_SHARE.value,
            root_path_or_remote="/home/Ext_HDD",
            status="ready",
            options_json=encode_json({}),
        )

        with self.assertRaisesRegex(Exception, "нужен уже смонтированный путь macOS из `/Volumes`"):
            validate_job_route(source, self.target, source_path="")

    def test_validate_job_route_accepts_synology_share_root_plus_subfolder(self):
        source = StorageProfile(
            name="Synology",
            profile_type=ProfileType.SYNOLOGY_SHARE.value,
            root_path_or_remote="/Volumes/home",
            status="ready",
            options_json=encode_json({}),
        )

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
        ):
            source_path, _ = validate_job_route(source, self.target, source_path="Ext_HDD")

        self.assertEqual(source_path, "/Volumes/home/Ext_HDD")

    def test_validate_job_route_accepts_synology_leaf_root_without_subfolder(self):
        source = StorageProfile(
            name="Synology",
            profile_type=ProfileType.SYNOLOGY_SHARE.value,
            root_path_or_remote="/Volumes/home/Ext_HDD",
            status="ready",
            options_json=encode_json({}),
        )

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
        ):
            source_path, _ = validate_job_route(source, self.target, source_path="")

        self.assertEqual(source_path, "/Volumes/home/Ext_HDD")

    def test_retry_run_restarts_interrupted_job(self):
        job = self._create_job()
        run = RunHistory(
            job_id=job.id,
            status=RunStatus.INTERRUPTED.value,
            summary="Прервано",
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        self.runner.enqueue_job = lambda job_id, initiated_by="manual": (True, "Задача запущена.")

        job_id, message = retry_run(self.session, run.id, self.runner)

        self.assertEqual(job_id, job.id)
        self.assertEqual(message, "Задача запущена.")

    def test_retry_run_rejects_non_interrupted_run(self):
        job = self._create_job()
        run = RunHistory(
            job_id=job.id,
            status=RunStatus.SUCCESS.value,
            summary="Успешно",
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        with self.assertRaises(RunRecoveryError):
            retry_run(self.session, run.id, self.runner)

    def test_execute_job_uses_human_readable_summary_for_missing_directory(self):
        job = self._create_job()
        runner = JobRunner()

        result = RcloneResult(
            returncode=3,
            stdout="",
            stderr='{"level":"error","msg":"error reading source root directory: directory not found"}\n'
            '{"level":"notice","msg":"Failed to copy: directory not found"}\n',
            command_preview="rclone copy /tmp/source yadisk:",
        )

        import app.services.jobs as jobs_module

        original_session_local = jobs_module.SessionLocal
        try:
            jobs_module.SessionLocal = lambda: self.session
            with patch("app.services.jobs.execute_with_progress", return_value=result):
                runner._execute_job(job.id, "manual", jobs_module.Event())
        finally:
            jobs_module.SessionLocal = original_session_local

        runs = self.session.query(RunHistory).filter(RunHistory.job_id == job.id).all()
        latest = runs[-1]
        self.assertEqual(latest.status, RunStatus.FAILED.value)
        self.assertIn("Источник или подпапка не найдены", latest.summary)


if __name__ == "__main__":
    unittest.main()

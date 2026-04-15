from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import ProfileType, StorageProfile, SyncJob
from app.schemas import SyncJobPayload
from app.services.common import encode_json
from unittest.mock import patch

from app.models import RunHistory, RunStatus
from app.services.jobs import (
    JobConflictError,
    JobDeleteError,
    JobRunner,
    RunRecoveryError,
    RunSnapshot,
    delete_job,
    preview_job,
    retry_run,
    validate_job_route,
    validate_job_endpoints,
)
from app.services.rclone import RcloneResult


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

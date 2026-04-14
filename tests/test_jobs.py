from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import ProfileType, StorageProfile, SyncJob
from app.services.common import encode_json
from unittest.mock import patch

from app.models import RunHistory, RunStatus
from app.services.jobs import JobDeleteError, RunRecoveryError, RunSnapshot, delete_job, preview_job, retry_run


class DummyRunner:
    def __init__(self) -> None:
        self.active_runs: dict[int, object] = {}
        self.synced: list[int] = []

    def sync_job(self, job_id: int) -> None:
        self.synced.append(job_id)


class JobsServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.session = TestingSession()
        self.runner = DummyRunner()

        source = StorageProfile(
            name="Source",
            profile_type=ProfileType.LOCAL_FOLDER.value,
            root_path_or_remote="/tmp/source",
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


if __name__ == "__main__":
    unittest.main()

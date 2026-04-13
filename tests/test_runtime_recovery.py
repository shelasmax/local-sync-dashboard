from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import ProfileType, RunHistory, RunStatus, StorageProfile, SyncJob
from app.services.common import encode_json
from app.services.jobs import JobRunner, RunSnapshot


class RuntimeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.original_data_dir = settings.data_dir
        self.tmp_dir = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self.tmp_dir.name)
        settings.ensure_runtime_dirs()

        self.engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.session = TestingSession()

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

        job = SyncJob(
            name="Source -> Target",
            source_profile_id=source.id,
            target_profile_id=target.id,
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
        self.job = job

        run = RunHistory(
            job_id=job.id,
            status=RunStatus.RUNNING.value,
            summary="Запуск вручную.",
            started_at=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        self.run = run

    def tearDown(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)
        settings.data_dir = self.original_data_dir
        settings.ensure_runtime_dirs()
        self.tmp_dir.cleanup()

    def test_recover_interrupted_run_from_snapshot(self):
        runner = JobRunner()
        snapshot = RunSnapshot(
            job_id=self.job.id,
            run_id=self.run.id,
            job_name=self.job.name,
            summary="Запуск вручную.",
            bytes_transferred=1024,
            total_bytes=4096,
            files_transferred=3,
            total_files=8,
        )
        runner._persist_snapshot(snapshot)

        from app.services.jobs import SessionLocal as JobsSessionLocal

        old_factory = JobsSessionLocal
        try:
            import app.services.jobs as jobs_module

            jobs_module.SessionLocal = lambda: self.session
            runner.recover_interrupted_runs()
        finally:
            jobs_module.SessionLocal = old_factory

        refreshed = self.session.get(RunHistory, self.run.id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, RunStatus.INTERRUPTED.value)
        self.assertEqual(refreshed.bytes_transferred, 1024)
        self.assertEqual(refreshed.files_transferred, 3)
        self.assertIsNotNone(refreshed.finished_at)
        self.assertIn("Последний известный прогресс", refreshed.summary)
        self.assertFalse((settings.runtime_dir / f"run-{self.run.id}.json").exists())


if __name__ == "__main__":
    unittest.main()

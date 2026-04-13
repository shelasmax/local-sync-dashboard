from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import ProfileType, StorageProfile, SyncJob
from app.services.common import encode_json
from app.services.jobs import JobDeleteError, RunSnapshot, delete_job


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


if __name__ == "__main__":
    unittest.main()

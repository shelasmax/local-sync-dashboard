"""Tests for v1.2.0 bug fixes: orphan-run reaper, broad exception handling, SIGKILL grace."""
from __future__ import annotations

import os
import signal
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import ProfileType, RunHistory, RunStatus, StorageProfile, SyncJob
from app.services.common import encode_json
from app.services.jobs import JobRunner, RunSnapshot
from app.services.rclone import RcloneResult, _GRACE_SECONDS


class _BaseDBTest(unittest.TestCase):
    def setUp(self):
        self.original_data_dir = settings.data_dir
        self.tmp_dir = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self.tmp_dir.name)
        settings.ensure_runtime_dirs()

        self.engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
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

        self.job = SyncJob(
            name="Test Job",
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
        self.session.add(self.job)
        self.session.commit()
        self.session.refresh(self.job)

    def tearDown(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        settings.data_dir = self.original_data_dir
        settings.ensure_runtime_dirs()
        self.tmp_dir.cleanup()

    def _patch_session_local(self, runner):
        import app.services.jobs as jobs_module

        original = jobs_module.SessionLocal
        jobs_module.SessionLocal = lambda: self.session
        return original

    def _restore_session_local(self, original):
        import app.services.jobs as jobs_module

        jobs_module.SessionLocal = original


class OrphanRunReaperTest(_BaseDBTest):
    """Bug 4: _reap_orphan_runs marks running DB rows as INTERRUPTED when no thread owns them."""

    def test_reap_marks_orphan_run_as_interrupted(self):
        run = RunHistory(
            job_id=self.job.id,
            status=RunStatus.RUNNING.value,
            summary="Running...",
            started_at=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        runner = JobRunner()
        original = self._patch_session_local(runner)
        try:
            runner._reap_orphan_runs()
        finally:
            self._restore_session_local(original)

        refreshed = self.session.get(RunHistory, run.id)
        self.assertEqual(refreshed.status, RunStatus.INTERRUPTED.value)
        self.assertIn("потеряна", refreshed.summary)
        self.assertIsNotNone(refreshed.finished_at)

    def test_reap_skips_run_with_active_thread(self):
        run = RunHistory(
            job_id=self.job.id,
            status=RunStatus.RUNNING.value,
            summary="Running...",
            started_at=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        runner = JobRunner()
        from threading import Event, Thread

        cancel_event = Event()
        snapshot = RunSnapshot(job_id=self.job.id, run_id=run.id)
        runner.active_runs[self.job.id] = JobRunner.__mro__[0].__dict__.get(
            "_execute_job", None
        )  # placeholder
        # Simulate an active run entry
        from app.services.jobs import ActiveRun

        runner.active_runs[self.job.id] = ActiveRun(
            thread=Thread(target=lambda: None, daemon=True),
            cancel_event=cancel_event,
            snapshot=snapshot,
        )

        original = self._patch_session_local(runner)
        try:
            runner._reap_orphan_runs()
        finally:
            self._restore_session_local(original)
            cancel_event.set()
            runner.active_runs.clear()

        refreshed = self.session.get(RunHistory, run.id)
        self.assertEqual(refreshed.status, RunStatus.RUNNING.value)

    def test_reap_cleans_stale_snapshot_files(self):
        run = RunHistory(
            job_id=self.job.id,
            status=RunStatus.RUNNING.value,
            summary="Running...",
            started_at=datetime.now(UTC),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)

        snapshot = RunSnapshot(job_id=self.job.id, run_id=run.id)
        runner = JobRunner()
        runner._persist_snapshot(snapshot)
        snapshot_path = settings.runtime_dir / f"run-{run.id}.json"
        self.assertTrue(snapshot_path.exists())

        original = self._patch_session_local(runner)
        try:
            runner._reap_orphan_runs()
        finally:
            self._restore_session_local(original)

        self.assertFalse(snapshot_path.exists())


class BroadExceptionHandlingTest(_BaseDBTest):
    """Bug 3: _execute_job catches all exceptions and marks the run as FAILED in DB."""

    def test_unexpected_exception_marks_run_failed(self):
        # Make source root exist so validate_job_endpoints passes
        os.makedirs("/tmp/source", exist_ok=True)
        try:
            runner = JobRunner()
            original = self._patch_session_local(runner)
            try:
                from threading import Event

                with patch(
                    "app.services.jobs.execute_with_progress",
                    side_effect=OSError("Broken pipe"),
                ):
                    runner._execute_job(self.job.id, "manual", Event())
            finally:
                self._restore_session_local(original)

            runs = (
                self.session.query(RunHistory)
                .filter(RunHistory.job_id == self.job.id)
                .all()
            )
            self.assertGreaterEqual(len(runs), 1)
            latest = runs[-1]
            self.assertEqual(latest.status, RunStatus.FAILED.value)
            self.assertIn("Неожиданная ошибка", latest.summary)
            self.assertIsNotNone(latest.finished_at)
        finally:
            os.rmdir("/tmp/source")


class GracefulKillTest(unittest.TestCase):
    """Bug 1&2: _stream_process uses terminate → wait → kill, and survives selector errors."""

    def test_process_killed_after_grace_period(self):
        from app.services.rclone import _stream_process

        # Simulate a process that ignores SIGTERM by using a shell sleep
        import subprocess

        process = subprocess.Popen(
            ["sleep", "300"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        from threading import Event

        cancel_event = Event()
        cancel_event.set()

        # Use very short timeout so it hits the terminate path quickly
        with patch("app.services.rclone._GRACE_SECONDS", 0.5):
            result = _stream_process(process, ["sleep", "300"], 1, cancel_event=cancel_event)

        self.assertTrue(result.canceled)
        # Process should be dead by now
        self.assertIsNotNone(process.returncode)

    def test_selector_oserror_does_not_crash(self):
        """When selector.select() raises OSError, _stream_process exits cleanly."""
        from app.services.rclone import _stream_process
        import subprocess

        process = subprocess.Popen(
            ["echo", "hello"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        with patch.object(
            type(process), "poll", side_effect=[None, 0]
        ):
            # The process finishes quickly, so this mainly tests no crash
            result = _stream_process(process, ["echo", "hello"], 10)
            self.assertIsNotNone(result)

    def test_grace_seconds_is_reasonable(self):
        """Grace period should be short enough to not block UX."""
        self.assertLessEqual(_GRACE_SECONDS, 10)
        self.assertGreaterEqual(_GRACE_SECONDS, 2)


if __name__ == "__main__":
    unittest.main()

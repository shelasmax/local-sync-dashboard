from __future__ import annotations

from datetime import UTC, datetime
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.database import Base
from app.main import (
    interrupted_run_recovery_steps,
    job_pre_run_checklist,
    job_requires_manual_start_checklist,
    jobs_page,
    manual_start_page,
    job_transfer_warning,
    profile_diagnostic_actions,
    profile_diagnostic_hints,
    run_detail_page,
    runs_page,
)
from app.models import ProfileType, RunHistory, StorageProfile, SyncJob
from app.services.common import encode_json


class MainHelpersTest(unittest.TestCase):
    def make_profile(self, profile_type: ProfileType, name: str) -> StorageProfile:
        return StorageProfile(
            name=name,
            profile_type=profile_type.value,
            root_path_or_remote=name,
            status="ready",
            options_json="{}",
        )

    def test_job_transfer_warning_for_two_remote_profiles(self):
        source = self.make_profile(ProfileType.S3_REMOTE, "s3:")
        target = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")

        warning = job_transfer_warning(source, target)

        self.assertIn("через локальный Mac", warning)

    def test_job_transfer_warning_for_one_remote_profile(self):
        source = self.make_profile(ProfileType.LOCAL_FOLDER, "/tmp/source")
        target = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")

        warning = job_transfer_warning(source, target)

        self.assertIn("выполняется локально", warning)

    def test_job_transfer_warning_for_two_local_profiles(self):
        source = self.make_profile(ProfileType.LOCAL_FOLDER, "/tmp/source")
        target = self.make_profile(ProfileType.SYNOLOGY_SHARE, "/Volumes/nas")

        self.assertEqual(job_transfer_warning(source, target), "")

    def test_job_pre_run_checklist_for_remote_route(self):
        source = self.make_profile(ProfileType.S3_REMOTE, "s3:")
        target = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")

        checklist = job_pre_run_checklist(source, target)

        self.assertGreaterEqual(len(checklist), 3)
        self.assertTrue(any("sleep" in item for item in checklist))

    def test_job_pre_run_checklist_for_local_route(self):
        source = self.make_profile(ProfileType.LOCAL_FOLDER, "/tmp/source")
        target = self.make_profile(ProfileType.SYNOLOGY_SHARE, "/Volumes/nas")

        self.assertEqual(job_pre_run_checklist(source, target), [])

    def test_interrupted_run_recovery_steps_for_remote_route(self):
        source = self.make_profile(ProfileType.S3_REMOTE, "s3:")
        target = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")
        job = SyncJob(
            name="Cloud copy",
            source_profile=source,
            target_profile=target,
            source_profile_id=1,
            target_profile_id=2,
            source_path="",
            target_path="",
            schedule="manual",
            mode="copy",
            filters_json="[]",
        )
        run = RunHistory(
            job_id=1,
            status="interrupted",
            summary="Прервано",
            started_at=datetime.now(UTC),
            job=job,
        )

        steps = interrupted_run_recovery_steps(run)

        self.assertTrue(any("не уснет" in item for item in steps))
        self.assertTrue(any("Повторить с дельты" in item for item in steps))

    def test_job_requires_manual_start_checklist_for_remote_route(self):
        source = self.make_profile(ProfileType.S3_REMOTE, "s3:")
        target = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")
        job = SyncJob(
            name="Cloud copy",
            source_profile=source,
            target_profile=target,
            source_profile_id=1,
            target_profile_id=2,
            source_path="",
            target_path="",
            schedule="manual",
            mode="copy",
            filters_json="[]",
        )

        self.assertTrue(job_requires_manual_start_checklist(job))

    def test_job_requires_manual_start_checklist_for_local_route(self):
        source = self.make_profile(ProfileType.LOCAL_FOLDER, "/tmp/source")
        target = self.make_profile(ProfileType.SYNOLOGY_SHARE, "/Volumes/nas")
        job = SyncJob(
            name="Local copy",
            source_profile=source,
            target_profile=target,
            source_profile_id=1,
            target_profile_id=2,
            source_path="",
            target_path="",
            schedule="manual",
            mode="copy",
            filters_json="[]",
        )

        self.assertFalse(job_requires_manual_start_checklist(job))

    def test_profile_diagnostic_hints_for_missing_s3_remote(self):
        profile = self.make_profile(ProfileType.S3_REMOTE, "missingremote:bucket")
        profile.status = "unreachable"
        profile.options_json = '{"endpoint":"https://s3.example.com","_diagnostics":{"message":"remote not found"}}'

        hints = profile_diagnostic_hints(profile, configured_remotes={"yadisk"})

        self.assertTrue(any("rclone remote" in item for item in hints))

    def test_profile_diagnostic_hints_for_yandex_auth_problem(self):
        profile = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")
        profile.status = "unreachable"
        profile.options_json = '{"_diagnostics":{"message":"401 Unauthorized"}}'

        hints = profile_diagnostic_hints(profile, configured_remotes={"yadisk"})

        self.assertTrue(any("OAuth" in item or "переподключ" in item for item in hints))

    def test_profile_diagnostic_actions_for_missing_s3_remote(self):
        profile = self.make_profile(ProfileType.S3_REMOTE, "missingremote:bucket")
        profile.status = "unreachable"
        profile.options_json = '{"endpoint":"https://s3.example.com","_diagnostics":{"message":"remote not found"}}'

        actions = profile_diagnostic_actions(profile, configured_remotes={"yadisk"})

        labels = {item["label"] for item in actions}
        self.assertIn("Открыть setup", labels)
        self.assertIn("Исправить профиль", labels)

    def test_profile_diagnostic_actions_for_yandex_auth_problem(self):
        profile = self.make_profile(ProfileType.YANDEX_REMOTE, "yadisk:")
        profile.status = "unreachable"
        profile.options_json = '{"_diagnostics":{"message":"401 Unauthorized"}}'

        actions = profile_diagnostic_actions(profile, configured_remotes={"yadisk"})

        labels = {item["label"] for item in actions}
        self.assertIn("Открыть setup", labels)
        self.assertIn("Исправить профиль", labels)
        self.assertIn("Проверить после исправления", labels)


class MainPagesRecoveryUxTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.session = TestingSession()

        source = StorageProfile(
            name="Source Remote",
            profile_type=ProfileType.S3_REMOTE.value,
            root_path_or_remote="s3remote:bucket",
            status="ready",
            options_json=encode_json({}),
        )
        target = StorageProfile(
            name="Target Remote",
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
            name="Night copy",
            source_profile_id=source.id,
            target_profile_id=target.id,
            source_path="photos",
            target_path="backup",
            schedule="manual",
            enabled=True,
            mode="copy",
            filters_json="[]",
            verify_checksum=False,
        )
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)

        run = RunHistory(
            job_id=job.id,
            status="interrupted",
            summary="Прервано после потери сети.",
            started_at=datetime.now(UTC),
            bytes_transferred=1024,
            files_transferred=3,
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        self.run = run
        self.job = job

    def tearDown(self):
        self.session.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _request(self, path: str, query_string: bytes = b"") -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": query_string,
                "headers": [],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
        )

    def test_jobs_page_shows_recovery_and_pre_run_copy(self):
        request = self._request(
            "/jobs",
            query_string=f"source_profile_id={self.job.source_profile_id}&target_profile_id={self.job.target_profile_id}".encode(),
        )
        fake_preview = type(
            "Preview",
            (),
            {
                "job_id": self.job.id,
                "job_name": self.job.name,
                "source": "s3remote:bucket/photos",
                "target": "yadisk:backup",
                "source_files": 12,
                "source_bytes": 4096,
                "size_command_preview": "rclone size s3remote:bucket/photos --json",
                "command_preview": "rclone copy --dry-run",
                "dry_run_result": type(
                    "DryRunResult",
                    (),
                    {
                        "ok": True,
                        "stdout": "dry-run output",
                        "stderr": "",
                    },
                )(),
            },
        )()
        with patch("app.main.preview_job", return_value=fake_preview):
            response = jobs_page(request, preview_job_id=self.job.id, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Перед длинным запуском проверьте", body)
        self.assertIn("recovery в этом проекте делается через повторный `copy` с дельты", body)
        self.assertIn("Проверьте причину прерывания", body)
        self.assertIn("Checklist и запуск", body)

    def test_runs_page_shows_recovery_explanation(self):
        request = self._request("/runs")
        response = runs_page(request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Как читать recovery на этой странице", body)
        self.assertIn("без `re-attach` к старому процессу", body)
        self.assertIn("Checklist и запуск", body)

    def test_run_detail_page_shows_recovery_steps(self):
        request = self._request(f"/runs/{self.run.id}")
        response = run_detail_page(self.run.id, request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Запуск был прерван.", body)
        self.assertIn("Для длинного повторного запуска выберите окно", body)
        self.assertIn("Открыть checklist перед повтором", body)

    def test_manual_start_page_shows_explicit_checklist_and_cta(self):
        request = self._request(f"/jobs/{self.job.id}/start")
        response = manual_start_page(self.job.id, request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Проверка перед ручным запуском", body)
        self.assertIn("Перед нажатием «Запустить copy сейчас» проверьте всё ниже", body)
        self.assertIn("Запустить copy сейчас", body)
        self.assertIn("Сначала сделать Dry-run", body)


if __name__ == "__main__":
    unittest.main()

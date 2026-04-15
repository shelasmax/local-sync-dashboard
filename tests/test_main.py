from __future__ import annotations

from datetime import UTC, datetime
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from fastapi.routing import APIRoute

from app.database import Base
from app.main import (
    app,
    api_browse_profile,
    api_create_job,
    build_job_form_query,
    create_job_html,
    index,
    interrupted_run_recovery_steps,
    job_pre_run_checklist,
    job_requires_manual_start_checklist,
    jobs_page,
    manual_start_page,
    job_transfer_warning,
    prefill_job_payload,
    profile_diagnostic_actions,
    profile_diagnostic_hints,
    profile_diagnostic_runbook,
    profiles_page,
    run_detail_page,
    runs_archive_page,
    runs_page,
    setup_page,
    update_job_html,
)
from app.models import ProfileType, RunHistory, StorageProfile, SyncJob
from app.schemas import SyncJobPayload
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

    def test_profile_diagnostic_runbook_for_missing_remote(self):
        profile = self.make_profile(ProfileType.S3_REMOTE, "missingremote:bucket")
        profile.status = "unreachable"
        profile.options_json = '{"_diagnostics":{"message":"remote not found"}}'

        runbook = profile_diagnostic_runbook(profile, configured_remotes={"yadisk"})

        self.assertIsNotNone(runbook)
        assert runbook is not None
        self.assertEqual(runbook["href"], "/ops#ops-remote-not-found")

    def test_profile_diagnostic_runbook_for_synology_path_problem(self):
        profile = self.make_profile(ProfileType.SYNOLOGY_SHARE, "/home/Ext_HDD")
        profile.status = "unreachable"
        profile.options_json = '{"_diagnostics":{"message":"directory not found"}}'

        runbook = profile_diagnostic_runbook(profile, configured_remotes=set())

        self.assertIsNotNone(runbook)
        assert runbook is not None
        self.assertEqual(runbook["href"], "/ops#ops-synology-path")

    def test_profiles_page_shows_synology_mount_path_hint(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(engine)
        session = TestingSession()
        try:
            request = Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/profiles",
                    "raw_path": b"/profiles",
                    "query_string": b"",
                    "headers": [],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                }
            )
            response = profiles_page(request, session=session)
            body = response.body.decode()
        finally:
            session.close()
            Base.metadata.drop_all(engine)
            engine.dispose()

        self.assertIn("Как указывать путь для Synology", body)
        self.assertIn("/Volumes/home", body)

    def test_profiles_page_shows_ops_runbook_link_for_problem_profile(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(engine)
        session = TestingSession()
        problem = StorageProfile(
            name="Broken S3",
            profile_type=ProfileType.S3_REMOTE.value,
            root_path_or_remote="missingremote:bucket",
            status="unreachable",
            options_json='{"_diagnostics":{"message":"remote not found"}}',
        )
        session.add(problem)
        session.commit()
        try:
            request = Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/profiles",
                    "raw_path": b"/profiles",
                    "query_string": b"",
                    "headers": [],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                }
            )
            with patch("app.main.collect_setup_diagnostics", return_value=type("Diagnostics", (), {"remotes": [], "issues": [], "local_candidates": {"icloud": [], "google_drive": [], "mounted_volumes": []}, "rclone_available": True})()):
                response = profiles_page(request, session=session)
                body = response.body.decode()
        finally:
            session.close()
            Base.metadata.drop_all(engine)
            engine.dispose()

        self.assertIn("Сценарий missing remote", body)
        self.assertIn("/ops#ops-remote-not-found", body)

    def test_profiles_page_falls_back_when_setup_diagnostics_fail(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(engine)
        session = TestingSession()
        try:
            request = Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/profiles",
                    "raw_path": b"/profiles",
                    "query_string": b"",
                    "headers": [],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                }
            )
            with patch("app.main.collect_setup_diagnostics", side_effect=RuntimeError("boom")):
                response = profiles_page(request, session=session)
                body = response.body.decode()
        finally:
            session.close()
            Base.metadata.drop_all(engine)
            engine.dispose()

        self.assertIn("Профили хранилищ", body)

    def test_setup_page_shows_synology_mount_path_hint(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(engine)
        session = TestingSession()
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/setup",
                "raw_path": b"/setup",
                "query_string": b"",
                "headers": [],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
        )
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "rclone_available": True,
                "remotes": [],
                "local_candidates": {"icloud": [], "google_drive": [], "mounted_volumes": ["/Volumes/home"]},
                "issues": [],
            },
        )()
        try:
            with patch("app.main.collect_setup_diagnostics", return_value=diagnostics):
                response = setup_page(request, session=session)
                body = response.body.decode()
        finally:
            session.close()
            Base.metadata.drop_all(engine)
            engine.dispose()

        self.assertIn("Используйте mount path из `/Volumes`", body)
        self.assertIn("/Volumes/home/Ext_HDD", body)


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
        self.assertIn("Control Room", body)
        self.assertIn("Проверьте причину прерывания", body)
        self.assertIn("Checklist и запуск", body)
        self.assertIn("Открыть лог", body)

    def test_runs_page_shows_recovery_explanation(self):
        request = self._request("/runs")
        response = runs_page(request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Прерванные запуски", body)
        self.assertIn("последний interrupted run", body)
        self.assertIn("Checklist и запуск", body)
        self.assertIn("Архив запусков", body)
        self.assertIn('id="toggle-recovery-runs"', body)
        self.assertNotIn("Лента запусков", body)

    def test_index_page_shows_toggle_buttons_and_single_recovery_run(self):
        second_run = RunHistory(
            job_id=self.job.id,
            status="interrupted",
            summary="Прервано после sleep.",
            started_at=datetime.now(UTC),
            bytes_transferred=2048,
            files_transferred=5,
        )
        self.session.add(second_run)
        self.session.commit()

        request = self._request("/")
        response = index(request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="toggle-index-incidents"', body)
        self.assertIn('id="toggle-index-recovery"', body)
        self.assertEqual(body.count("Все recovery-сценарии"), 1)

    def test_runs_archive_page_shows_pagination_and_filters(self):
        extra_runs = [
            RunHistory(
                job_id=self.job.id,
                status="success",
                summary=f"Archived #{index}",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            for index in range(25)
        ]
        self.session.add_all(extra_runs)
        self.session.commit()

        request = self._request("/runs/archive")
        response = runs_archive_page(request, page=2, status="success", session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Архив запусков", body)
        self.assertIn("Страница 2 из 2", body)
        self.assertNotIn("Следующая страница", body)
        self.assertIn("Удалить из архива", body)

    def test_runs_operational_sections_show_only_latest_interrupted(self):
        second_run = RunHistory(
            job_id=self.job.id,
            status="interrupted",
            summary="Прервано после sleep.",
            started_at=datetime.now(UTC),
            bytes_transferred=2048,
            files_transferred=5,
        )
        self.session.add(second_run)
        self.session.commit()

        request = self._request("/runs")
        response = runs_page(request, session=self.session)
        body = response.body.decode()

        self.assertEqual(body.count("Повторить с дельты"), 1)

    def test_runs_page_shows_active_snapshot_even_without_running_db_row(self):
        request = self._request("/runs")
        snapshot = {
            self.job.id: {
                "job_id": self.job.id,
                "run_id": self.run.id,
                "job_name": self.job.name,
                "status": "running",
                "summary": "Идет перенос.",
                "command_preview": "rclone copy ...",
                "started_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                "bytes_transferred": 2048,
                "total_bytes": 4096,
                "remaining_bytes": 2048,
                "files_transferred": 2,
                "total_files": 4,
                "speed": 512,
                "eta_seconds": 60,
                "listed": 0,
                "checks": 0,
                "total_checks": 0,
                "progress_percent": 50.0,
                "active_items": [],
            }
        }
        with patch("app.main.job_runner.active_run_snapshots", return_value=snapshot):
            response = runs_page(request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Активные запуски", body)
        self.assertIn("Идет перенос.", body)
        self.assertIn('id="toggle-active-runs"', body)

    def test_runs_page_shows_empty_active_state_and_incidents_without_live_snapshot(self):
        request = self._request("/runs")
        failed_run = RunHistory(
            job_id=self.job.id,
            status="failed",
            summary="rclone завершился с кодом 3.",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        self.session.add(failed_run)
        self.session.commit()

        with patch("app.main.job_runner.active_run_snapshots", return_value={}):
            response = runs_page(request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Сейчас live-запусков нет", body)
        self.assertIn("Прерванные запуски", body)
        self.assertIn("Последние ошибки и блокировки", body)
        self.assertIn("rclone завершился с кодом 3.", body)

    def test_run_detail_page_shows_recovery_steps(self):
        request = self._request(f"/runs/{self.run.id}")
        response = run_detail_page(self.run.id, request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Запуск был прерван", body)
        self.assertIn("Для длинного повторного запуска выберите окно", body)
        self.assertIn("Checklist и запуск", body)

    def test_manual_start_page_shows_explicit_checklist_and_cta(self):
        request = self._request(f"/jobs/{self.job.id}/start")
        response = manual_start_page(self.job.id, request, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Проверка перед ручным запуском", body)
        self.assertIn("Перед нажатием «Запустить»", body)
        self.assertIn("Запустить copy сейчас", body)
        self.assertIn("Сначала сделать Dry-run", body)


class MainRoutingRegressionTest(unittest.TestCase):
    def test_runs_archive_route_registered_before_run_detail(self):
        paths = [route.path for route in app.routes if isinstance(route, APIRoute)]
        self.assertLess(paths.index("/runs/archive"), paths.index("/runs/{run_id}"))


class MainCreateJobErrorHandlingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.source_root = os.path.join(self.tempdir.name, "source")
        self.target_root = os.path.join(self.tempdir.name, "target")
        os.makedirs(self.source_root, exist_ok=True)
        os.makedirs(self.target_root, exist_ok=True)
        self.engine = create_engine("sqlite:///:memory:", future=True)
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.session = TestingSession()

        source = StorageProfile(
            name="Source",
            profile_type=ProfileType.LOCAL_FOLDER.value,
            root_path_or_remote=self.source_root,
            status="ready",
            options_json=encode_json({}),
        )
        target = StorageProfile(
            name="Target",
            profile_type=ProfileType.LOCAL_FOLDER.value,
            root_path_or_remote=self.target_root,
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

    def test_create_job_html_redirects_back_on_validation_error(self):
        response = create_job_html(
            name="x",
            source_profile_id=self.source.id,
            source_path="photos/raw",
            target_profile_id=self.target.id,
            target_path="backup/2026",
            schedule="weekly",
            enabled=True,
            bandwidth_limit="8M",
            verify_checksum=True,
            filters="+ *.jpg",
            session=self.session,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/jobs?error=", response.headers["location"])
        self.assertIn("source_path=photos%2Fraw", response.headers["location"])
        self.assertIn("target_path=backup%2F2026", response.headers["location"])
        self.assertIn("bandwidth_limit=8M", response.headers["location"])
        self.assertIn("verify_checksum=true", response.headers["location"])
        self.assertIn("enabled=true", response.headers["location"])
        self.assertIn("error_field=name", response.headers["location"])

    def test_create_job_html_redirects_back_on_duplicate_name(self):
        payload = SyncJobPayload(
            name="Daily copy",
            source_profile_id=self.source.id,
            source_path="",
            target_profile_id=self.target.id,
            target_path="",
            schedule="manual",
            enabled=True,
            filters=[],
        )
        api_create_job(payload, session=self.session)

        response = create_job_html(
            name="Daily copy",
            source_profile_id=self.source.id,
            source_path="",
            target_profile_id=self.target.id,
            target_path="",
            schedule="manual",
            enabled=True,
            bandwidth_limit="",
            verify_checksum=False,
            filters="",
            session=self.session,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/jobs?error=", response.headers["location"])
        self.assertIn("name=Daily+copy", response.headers["location"])
        self.assertIn("error_field=name", response.headers["location"])

    def test_prefill_job_payload_accepts_name_from_error_redirect(self):
        query = build_job_form_query(
            error="oops",
            name="Night copy",
            source_profile_id=self.source.id,
            source_path="photos",
            target_profile_id=self.target.id,
            target_path="backup",
            schedule="weekly",
            bandwidth_limit="8M",
            verify_checksum=True,
            filters="+ *.jpg",
            enabled=True,
        )
        payload = prefill_job_payload(dict(parse_qsl(query)))

        self.assertEqual(payload["name"], "Night copy")
        self.assertEqual(payload["source_path"], "photos")
        self.assertEqual(payload["target_path"], "backup")
        self.assertEqual(payload["schedule"], "weekly")
        self.assertEqual(payload["bandwidth_limit"], "8M")
        self.assertTrue(payload["verify_checksum"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["error_field"], "")

    def test_build_job_form_query_omits_empty_edit_and_clone_ids(self):
        query = build_job_form_query(
            error="oops",
            name="Night copy",
            source_profile_id=self.source.id,
            source_path="photos",
            target_profile_id=self.target.id,
            target_path="backup",
            schedule="weekly",
            bandwidth_limit="8M",
            verify_checksum=True,
            filters="+ *.jpg",
            enabled=True,
        )

        params = dict(parse_qsl(query))

        self.assertNotIn("edit_job_id", params)
        self.assertNotIn("clone_job_id", params)

    def test_jobs_page_prefills_edit_mode(self):
        job = SyncJob(
            name="Editable job",
            source_profile_id=self.source.id,
            source_path="photos",
            target_profile_id=self.target.id,
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

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/jobs",
                "raw_path": b"/jobs",
                "query_string": f"edit_job_id={job.id}".encode(),
                "headers": [],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
        )
        response = jobs_page(request, edit_job_id=job.id, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Редактирование задачи", body)
        self.assertIn("Editable job", body)

    def test_jobs_page_prefills_clone_mode(self):
        job = SyncJob(
            name="Clone me",
            source_profile_id=self.source.id,
            source_path="photos",
            target_profile_id=self.target.id,
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

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/jobs",
                "raw_path": b"/jobs",
                "query_string": f"clone_job_id={job.id}".encode(),
                "headers": [],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
        )
        response = jobs_page(request, clone_job_id=job.id, session=self.session)
        body = response.body.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Новая задача на основе текущей", body)
        self.assertIn("Clone me (copy)", body)

    def test_update_job_html_updates_existing_job(self):
        payload = SyncJobPayload(
            name="Daily copy",
            source_profile_id=self.source.id,
            source_path="incoming",
            target_profile_id=self.target.id,
            target_path="archive",
            schedule="manual",
            enabled=True,
            filters=[],
        )
        job = api_create_job(payload, session=self.session)

        response = update_job_html(
            job_id=job.id,
            name="Daily copy updated",
            source_profile_id=self.source.id,
            source_path="incoming/final",
            target_profile_id=self.target.id,
            target_path="archive/final",
            schedule="weekly",
            enabled=False,
            bandwidth_limit="8M",
            verify_checksum=True,
            filters="+ *.jpg",
            session=self.session,
        )

        self.assertEqual(response.status_code, 303)
        updated = self.session.get(SyncJob, job.id)
        assert updated is not None
        self.assertEqual(updated.name, "Daily copy updated")
        self.assertEqual(updated.source_path, "incoming/final")
        self.assertEqual(updated.schedule, "weekly")
        self.assertFalse(updated.enabled)

    def test_api_browse_profile_lists_local_subdirectories(self):
        os.makedirs(os.path.join(self.source_root, "photos"), exist_ok=True)
        os.makedirs(os.path.join(self.source_root, "docs"), exist_ok=True)

        payload = api_browse_profile(self.source.id, session=self.session)

        self.assertEqual(payload["profile_id"], self.source.id)
        self.assertEqual(payload["effective_path"], self.source_root)
        self.assertIn("photos", payload["entries"])
        self.assertIn("docs", payload["entries"])


if __name__ == "__main__":
    unittest.main()

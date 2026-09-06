"""Render static screenshot fixtures using real templates and synthetic data.

Run: .venv/bin/python -m scripts.render_public_demo (from the repository root). Output is written to
output/playwright/demo. No scheduler, rclone process, credentials, or real app
state is used. The generated site is for screenshots, not an operational demo.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = root / "output" / "playwright" / "demo"
    output.mkdir(parents=True, exist_ok=True)
    # Set before importing the app: database.py creates runtime directories.
    with tempfile.TemporaryDirectory(prefix="local-sync-public-demo-") as data_dir:
        os.environ["LOCAL_SYNC_DATA_DIR"] = data_dir
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from starlette.requests import Request

        import app.main as ui
        from app.database import Base
        from app.models import AppSettings, RunHistory, StorageProfile, SyncJob
        from app.services.jobs import RunSnapshot
        from app.services.system import SetupDiagnostics

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        timestamp = datetime(2026, 4, 16, 10, 30, tzinfo=UTC)
        with Session(engine, expire_on_commit=False) as session:
            s3 = StorageProfile(id=1, name="S3 · Media archive", profile_type="s3_remote",
                                root_path_or_remote="s3:media", status="ready")
            disk = StorageProfile(id=2, name="Yandex Disk · Copy", profile_type="yandex_remote",
                                  root_path_or_remote="disk:", status="ready")
            nas = StorageProfile(id=3, name="Synology · Photo library", profile_type="synology_share",
                                 root_path_or_remote="/Volumes/demo-photos", status="unreachable",
                                 options_json=json.dumps({"_diagnostics": {
                                     "message": "Папка не смонтирована. Подключите том в Finder.",
                                     "checked_at": timestamp.isoformat(), "ok": False}}))
            session.add_all([s3, disk, nas, AppSettings(id=1)])
            session.flush()
            jobs = [
                SyncJob(id=1, name="Media archive → Yandex Disk", source_profile=s3,
                        target_profile=disk, source_path="photos", target_path="Photos",
                        schedule="daily", rclone_transfers=8, rclone_checkers=16, rclone_fast_list=True),
                SyncJob(id=2, name="Synology → Yandex Disk", source_profile=nas,
                        target_profile=disk, target_path="Synology", schedule="weekly", enabled=False),
                SyncJob(id=3, name="Project files → Yandex Disk", source_profile=s3,
                        target_profile=disk, source_path="projects", target_path="Projects",
                        schedule="manual"),
            ]
            session.add_all(jobs)
            session.flush()
            command = "rclone copy s3:media/photos disk:Photos --transfers 8 --checkers 16 --fast-list"
            session.add(RunHistory(id=1, job=jobs[0], status="running", started_at=timestamp-timedelta(hours=1),
                                   summary="Копирование выполняется.", command_preview=command))
            session.add(RunHistory(id=2, job=jobs[2], status="failed", started_at=timestamp-timedelta(days=1, hours=6),
                                   finished_at=timestamp-timedelta(days=1), bytes_transferred=18*1024**3,
                                   files_transferred=12480, stderr="Timed out after 21600 seconds",
                                   summary="Превышен лимит времени: 6 ч. Перенесено 12480 файлов, 18.0 GiB.",
                                   command_preview="rclone copy s3:media/projects disk:Projects"))
            for i in range(3, 8):
                session.add(RunHistory(id=i, job=jobs[0 if i % 2 else 2], status="success",
                                       started_at=timestamp-timedelta(days=i, hours=2),
                                       finished_at=timestamp-timedelta(days=i),
                                       files_transferred=2400+i*120, bytes_transferred=(4+i)*1024**3,
                                       summary="Копирование завершено. Ошибок нет."))
            session.commit()
            live = RunSnapshot(job_id=1, run_id=1, job_name=jobs[0].name, command_preview=command,
                               started_at=timestamp-timedelta(hours=1), updated_at=timestamp,
                               summary="Копирование выполняется.", bytes_transferred=36*1024**3,
                               total_bytes=48*1024**3, files_transferred=18432, total_files=24576,
                               speed=12*1024**2, eta_seconds=1024, checks=32768, total_checks=32768,
                               listed=57344).to_dict()
            snapshots = {1: live}
            status = {"scheduler_running": True, "rclone_available": True, "active_jobs": 1,
                      "active_runs": snapshots, "jobs_count": 3, "profiles_count": 3, "runs_count": 7}
            diagnostics = SetupDiagnostics(True, ["s3", "disk"],
                                           {"icloud": [], "google_drive": [], "mounted_volumes": []},
                                           ["Подключите том /Volumes/demo-photos для задачи Synology."])
            pages = [("/", ui.index, {}), ("/jobs", ui.jobs_page, {}),
                     ("/profiles", ui.profiles_page, {}), ("/runs", ui.runs_page, {}),
                     ("/runs/1", ui.run_detail_page, {"run_id": 1}),
                     ("/runs/2", ui.run_detail_page, {"run_id": 2}),
                     ("/runs/archive", ui.runs_archive_page, {}),
                     ("/setup", ui.setup_page, {}), ("/ops", ui.ops_page, {})]
            with ExitStack() as stack:
                stack.enter_context(patch.object(ui, "collect_setup_diagnostics", return_value=diagnostics))
                stack.enter_context(patch.object(ui.job_runner, "status", return_value=status))
                stack.enter_context(patch.object(ui.job_runner, "active_run_snapshots", return_value=snapshots))
                stack.enter_context(patch("subprocess.run", side_effect=RuntimeError("External commands disabled in screenshot fixtures")))
                for path, handler, extra in pages:
                    request = Request({"type": "http", "method": "GET", "path": path,
                                       "query_string": b"", "headers": [], "app": ui.app,
                                       "scheme": "http", "server": ("127.0.0.1", 8765)})
                    response = handler(request=request, session=session, **extra)
                    folder = output / path.strip("/")
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / "index.html").write_bytes(response.body)
                api = output / "api" / "runtime" / "jobs"
                api.mkdir(parents=True, exist_ok=True)
                (api / "index.html").write_text(json.dumps(status), encoding="utf-8")
            shutil.copytree(root / "app" / "static", output / "static", dirs_exist_ok=True)
        engine.dispose()
    print(f"Rendered {len(pages)} static pages in {output}")


if __name__ == "__main__":
    main()

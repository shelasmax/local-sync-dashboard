from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from time import sleep
import shlex

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete, func, select

from app.config import settings
from app.database import SessionLocal
from app.models import AppSettings, RunHistory, RunStatus, StorageProfile, SyncJob
from app.schemas import SyncJobPayload
from app.services.common import decode_json, encode_json, is_temporary_network_error
from app.services.profiles import storage_endpoint
from app.services.rclone import RcloneError, build_copy_command, execute, is_available
from app.services.scheduling import build_trigger


def init_settings(session) -> AppSettings:
    app_settings = session.get(AppSettings, 1)
    if not app_settings:
        app_settings = AppSettings(
            id=1,
            log_retention_days=settings.default_log_retention_days,
            max_concurrent_runs=settings.default_max_concurrent_runs,
            default_retry_count=settings.default_retry_count,
            default_run_timeout_seconds=settings.default_run_timeout_seconds,
        )
        session.add(app_settings)
        session.commit()
        session.refresh(app_settings)
    return app_settings


def create_job(session, payload: SyncJobPayload) -> SyncJob:
    job = SyncJob(
        name=payload.name,
        source_profile_id=payload.source_profile_id,
        source_path=payload.source_path,
        target_profile_id=payload.target_profile_id,
        target_path=payload.target_path,
        schedule=payload.schedule,
        enabled=payload.enabled,
        mode=payload.mode,
        filters_json=encode_json(payload.filters),
        bandwidth_limit=payload.bandwidth_limit,
        verify_checksum=payload.verify_checksum,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@dataclass(slots=True)
class ActiveRun:
    thread: Thread
    cancel_event: Event


class JobRunner:
    def __init__(self) -> None:
        self.scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
        self.lock = Lock()
        self.active_runs: dict[int, ActiveRun] = {}
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.scheduler.start()
        self.started = True
        self.refresh_schedules()
        self.cleanup_old_runs()

    def shutdown(self) -> None:
        if self.started:
            self.scheduler.shutdown(wait=False)
        self.started = False

    def refresh_schedules(self) -> None:
        with SessionLocal() as session:
            init_settings(session)
            jobs = session.scalars(select(SyncJob)).all()
        for job in jobs:
            self.sync_job(job.id)

    def sync_job(self, job_id: int) -> None:
        with SessionLocal() as session:
            job = session.get(SyncJob, job_id)
            if not job:
                existing = self.scheduler.get_job(f"job-{job_id}")
                if existing:
                    self.scheduler.remove_job(f"job-{job_id}")
                return
            existing = self.scheduler.get_job(f"job-{job_id}")
            if existing:
                self.scheduler.remove_job(f"job-{job_id}")
            if not job.enabled:
                return
            trigger = build_trigger(job.schedule, settings.scheduler_timezone)
            if trigger is None:
                return
            self.scheduler.add_job(
                self.enqueue_job,
                trigger=trigger,
                args=[job_id, "scheduled"],
                id=f"job-{job_id}",
                replace_existing=True,
            )

    def enqueue_job(self, job_id: int, initiated_by: str = "manual") -> tuple[bool, str]:
        with SessionLocal() as session:
            app_settings = init_settings(session)
            allowed_parallelism = app_settings.max_concurrent_runs

        with self.lock:
            if job_id in self.active_runs:
                return False, "Задача уже выполняется."
            if len(self.active_runs) >= allowed_parallelism:
                self._create_blocked_run(job_id, "Сейчас уже выполняется другая задача синхронизации.")
                return False, "Сейчас уже выполняется другая задача синхронизации."

            cancel_event = Event()
            thread = Thread(target=self._execute_job, args=(job_id, initiated_by, cancel_event), daemon=True)
            self.active_runs[job_id] = ActiveRun(thread=thread, cancel_event=cancel_event)
            thread.start()
        return True, "Задача запущена."

    def cancel_job(self, job_id: int) -> bool:
        with self.lock:
            active = self.active_runs.get(job_id)
            if not active:
                return False
            active.cancel_event.set()
        return True

    def _create_blocked_run(self, job_id: int, summary: str) -> None:
        with SessionLocal() as session:
            run = RunHistory(
                job_id=job_id,
                status=RunStatus.BLOCKED.value,
                summary=summary,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            session.add(run)
            session.commit()

    def _execute_job(self, job_id: int, initiated_by: str, cancel_event: Event) -> None:
        try:
            with SessionLocal() as session:
                app_settings = init_settings(session)
                job = session.get(SyncJob, job_id)
                if not job:
                    return
                if not job.enabled and initiated_by == "scheduled":
                    return

                source_profile = session.get(StorageProfile, job.source_profile_id)
                target_profile = session.get(StorageProfile, job.target_profile_id)
                if not source_profile or not target_profile:
                    self._create_blocked_run(job_id, "Не найден профиль источника или назначения.")
                    return

                source = storage_endpoint(source_profile, job.source_path)
                target = storage_endpoint(target_profile, job.target_path)
                filters = decode_json(job.filters_json, [])
                command = build_copy_command(
                    source=source,
                    target=target,
                    filters=filters,
                    bandwidth_limit=job.bandwidth_limit,
                    verify_checksum=job.verify_checksum,
                )

                initiated_labels = {
                    "manual": "вручную",
                    "scheduled": "по расписанию",
                }
                run = RunHistory(
                    job_id=job_id,
                    status=RunStatus.RUNNING.value,
                    summary=f"Запуск {initiated_labels.get(initiated_by, initiated_by)}.",
                    command_preview=shlex.join(command),
                    started_at=datetime.now(UTC),
                )
                session.add(run)
                session.commit()
                session.refresh(run)

                result = None
                attempts = max(1, app_settings.default_retry_count + 1)
                for attempt in range(1, attempts + 1):
                    try:
                        result = execute(
                            command=command,
                            timeout_seconds=app_settings.default_run_timeout_seconds,
                            cancel_event=cancel_event,
                        )
                    except RcloneError as exc:
                        result = None
                        run.status = RunStatus.FAILED.value
                        run.summary = str(exc)
                        run.stderr = str(exc)
                        run.finished_at = datetime.now(UTC)
                        session.commit()
                        return

                    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
                    if result.ok or result.canceled or not is_temporary_network_error(combined) or attempt == attempts:
                        break
                    sleep(3)

                if result is None:
                    return

                log_path = self._write_run_log(run.id, result.command_preview, result.stdout, result.stderr)
                run.command_preview = result.command_preview
                run.log_path = str(log_path)
                run.stdout = result.stdout
                run.stderr = result.stderr
                run.bytes_transferred = result.bytes_transferred
                run.files_transferred = result.files_transferred
                run.finished_at = datetime.now(UTC)

                if result.canceled:
                    run.status = RunStatus.CANCELED.value
                    run.summary = "Запуск отменен пользователем."
                elif result.ok:
                    run.status = RunStatus.SUCCESS.value
                    run.summary = (
                        f"Успешно завершено. Перенесено файлов: {result.files_transferred}. "
                        f"Перенесено байт: {result.bytes_transferred}."
                    )
                else:
                    run.status = RunStatus.FAILED.value
                    run.summary = f"rclone завершился с кодом {result.returncode}."
                session.commit()
        finally:
            with self.lock:
                self.active_runs.pop(job_id, None)
            self.cleanup_old_runs()

    def cleanup_old_runs(self) -> None:
        with SessionLocal() as session:
            app_settings = init_settings(session)
            cutoff = datetime.now(UTC) - timedelta(days=app_settings.log_retention_days)
            expired_runs = session.scalars(select(RunHistory).where(RunHistory.started_at < cutoff)).all()
            for run in expired_runs:
                if run.log_path:
                    log_path = Path(run.log_path)
                    if log_path.exists():
                        log_path.unlink(missing_ok=True)
            session.execute(delete(RunHistory).where(RunHistory.started_at < cutoff))
            session.commit()

    def status(self) -> dict[str, object]:
        with SessionLocal() as session:
            init_settings(session)
            jobs_count = session.scalar(select(func.count(SyncJob.id))) or 0
            profiles_count = session.scalar(select(func.count(StorageProfile.id))) or 0
            runs_count = session.scalar(select(func.count(RunHistory.id))) or 0
        return {
            "scheduler_running": self.started,
            "rclone_available": is_available(),
            "active_jobs": len(self.active_runs),
            "jobs_count": jobs_count,
            "profiles_count": profiles_count,
            "runs_count": runs_count,
        }

    def _write_run_log(self, run_id: int, command_preview: str, stdout: str, stderr: str) -> Path:
        settings.ensure_runtime_dirs()
        log_path = settings.logs_dir / f"run-{run_id}.log"
        log_path.write_text(
            "\n".join(
                [
                    f"command: {command_preview}",
                    "",
                    "stdout:",
                    stdout,
                    "",
                    "stderr:",
                    stderr,
                ]
            ),
            encoding="utf-8",
        )
        return log_path


job_runner = JobRunner()

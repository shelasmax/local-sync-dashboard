from __future__ import annotations

import json
from dataclasses import dataclass, field
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
from app.services.rclone import (
    RcloneError,
    RcloneProgress,
    build_copy_command,
    execute_with_progress,
    is_available,
)
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


class JobDeleteError(RuntimeError):
    pass


def delete_job(session, job_id: int, runner: "JobRunner") -> None:
    job = session.get(SyncJob, job_id)
    if not job:
        raise LookupError("Задача не найдена.")
    if job_id in runner.active_runs:
        raise JobDeleteError("Нельзя удалить активную задачу. Сначала остановите ее.")

    session.delete(job)
    session.commit()
    runner.sync_job(job_id)


@dataclass(slots=True)
class ActiveRun:
    thread: Thread
    cancel_event: Event
    snapshot: "RunSnapshot"


@dataclass(slots=True)
class RunSnapshot:
    job_id: int
    run_id: int | None = None
    job_name: str = ""
    status: str = RunStatus.RUNNING.value
    summary: str = ""
    command_preview: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    bytes_transferred: int = 0
    total_bytes: int = 0
    files_transferred: int = 0
    total_files: int = 0
    speed: float = 0
    eta_seconds: int | None = None
    listed: int = 0
    checks: int = 0
    total_checks: int = 0
    active_items: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        remaining_bytes = max(0, self.total_bytes - self.bytes_transferred) if self.total_bytes else 0
        progress_percent = 0
        if self.total_bytes > 0:
            progress_percent = max(0, min(100, round((self.bytes_transferred / self.total_bytes) * 100, 1)))
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "job_name": self.job_name,
            "status": self.status,
            "summary": self.summary,
            "command_preview": self.command_preview,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "bytes_transferred": self.bytes_transferred,
            "total_bytes": self.total_bytes,
            "remaining_bytes": remaining_bytes,
            "files_transferred": self.files_transferred,
            "total_files": self.total_files,
            "speed": self.speed,
            "eta_seconds": self.eta_seconds,
            "listed": self.listed,
            "checks": self.checks,
            "total_checks": self.total_checks,
            "progress_percent": progress_percent,
            "active_items": list(self.active_items),
        }


class JobRunner:
    def __init__(self) -> None:
        self.scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
        self.lock = Lock()
        self.active_runs: dict[int, ActiveRun] = {}
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.recover_interrupted_runs()
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
            snapshot = RunSnapshot(job_id=job_id)
            thread = Thread(target=self._execute_job, args=(job_id, initiated_by, cancel_event), daemon=True)
            self.active_runs[job_id] = ActiveRun(thread=thread, cancel_event=cancel_event, snapshot=snapshot)
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

                self._initialize_snapshot(
                    job_id=job_id,
                    run_id=run.id,
                    job_name=job.name,
                    summary=run.summary,
                    command_preview=run.command_preview or "",
                )

                result = None
                attempts = max(1, app_settings.default_retry_count + 1)
                for attempt in range(1, attempts + 1):
                    try:
                        result = execute_with_progress(
                            command=command,
                            timeout_seconds=app_settings.default_run_timeout_seconds,
                            progress_callback=lambda progress, current_job_id=job_id: self._apply_progress(current_job_id, progress),
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
                self._finalize_snapshot(job_id, run.status, run.summary, result.bytes_transferred, result.files_transferred)
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
            "active_runs": self.active_run_snapshots(),
            "jobs_count": jobs_count,
            "profiles_count": profiles_count,
            "runs_count": runs_count,
        }

    def active_run_snapshots(self) -> dict[int, dict[str, object]]:
        with self.lock:
            return {job_id: active.snapshot.to_dict() for job_id, active in self.active_runs.items()}

    def _initialize_snapshot(
        self,
        *,
        job_id: int,
        run_id: int,
        job_name: str,
        summary: str,
        command_preview: str,
    ) -> None:
        with self.lock:
            active = self.active_runs.get(job_id)
            if not active:
                return
            active.snapshot.run_id = run_id
            active.snapshot.job_name = job_name
            active.snapshot.summary = summary
            active.snapshot.command_preview = command_preview
            active.snapshot.updated_at = datetime.now(UTC)
            self._persist_snapshot(active.snapshot)

    def _apply_progress(self, job_id: int, progress: RcloneProgress) -> None:
        with self.lock:
            active = self.active_runs.get(job_id)
            if not active:
                return
            snapshot = active.snapshot
            snapshot.bytes_transferred = progress.bytes_transferred
            snapshot.total_bytes = progress.total_bytes
            snapshot.files_transferred = progress.files_transferred
            snapshot.total_files = progress.total_files
            snapshot.speed = progress.speed
            snapshot.eta_seconds = progress.eta_seconds
            snapshot.listed = progress.listed
            snapshot.checks = progress.checks
            snapshot.total_checks = progress.total_checks
            snapshot.active_items = list(progress.active_items or [])
            snapshot.updated_at = datetime.now(UTC)
            self._persist_snapshot(snapshot)

    def _finalize_snapshot(
        self,
        job_id: int,
        status: str,
        summary: str,
        bytes_transferred: int,
        files_transferred: int,
    ) -> None:
        with self.lock:
            active = self.active_runs.get(job_id)
            if not active:
                return
            active.snapshot.status = status
            active.snapshot.summary = summary
            active.snapshot.bytes_transferred = bytes_transferred
            active.snapshot.files_transferred = files_transferred
            active.snapshot.updated_at = datetime.now(UTC)
            self._remove_snapshot_file(active.snapshot)

    def recover_interrupted_runs(self) -> None:
        settings.ensure_runtime_dirs()
        snapshot_files = sorted(settings.runtime_dir.glob("run-*.json"))
        recovered_run_ids: set[int] = set()

        for snapshot_file in snapshot_files:
            snapshot = self._load_snapshot(snapshot_file)
            if not snapshot or snapshot.run_id is None:
                snapshot_file.unlink(missing_ok=True)
                continue
            self._mark_run_interrupted(snapshot)
            recovered_run_ids.add(snapshot.run_id)
            snapshot_file.unlink(missing_ok=True)

        with SessionLocal() as session:
            stale_runs = session.scalars(
                select(RunHistory).where(
                    RunHistory.status == RunStatus.RUNNING.value,
                    RunHistory.finished_at.is_(None),
                )
            ).all()
            for run in stale_runs:
                if run.id in recovered_run_ids:
                    continue
                run.status = RunStatus.INTERRUPTED.value
                run.summary = "Запуск был прерван из-за остановки приложения или перезагрузки Mac."
                run.finished_at = datetime.now(UTC)
            session.commit()

    def _mark_run_interrupted(self, snapshot: RunSnapshot) -> None:
        with SessionLocal() as session:
            run = session.get(RunHistory, snapshot.run_id)
            if not run or run.status != RunStatus.RUNNING.value:
                return
            run.status = RunStatus.INTERRUPTED.value
            run.finished_at = datetime.now(UTC)
            run.bytes_transferred = max(run.bytes_transferred, snapshot.bytes_transferred)
            run.files_transferred = max(run.files_transferred, snapshot.files_transferred)
            progress_summary = (
                f"Запуск был прерван. Последний известный прогресс: "
                f"{snapshot.files_transferred}/{snapshot.total_files or '?'} файлов, "
                f"{snapshot.bytes_transferred}/{snapshot.total_bytes or '?'} байт."
            )
            run.summary = progress_summary
            session.commit()

    def _snapshot_path(self, snapshot: RunSnapshot) -> Path:
        identifier = snapshot.run_id if snapshot.run_id is not None else f"job-{snapshot.job_id}"
        return settings.runtime_dir / f"run-{identifier}.json"

    def _persist_snapshot(self, snapshot: RunSnapshot) -> None:
        settings.ensure_runtime_dirs()
        path = self._snapshot_path(snapshot)
        path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def _remove_snapshot_file(self, snapshot: RunSnapshot) -> None:
        self._snapshot_path(snapshot).unlink(missing_ok=True)

    def _load_snapshot(self, path: Path) -> RunSnapshot | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return RunSnapshot(
                job_id=int(payload.get("job_id", 0)),
                run_id=int(payload["run_id"]) if payload.get("run_id") is not None else None,
                job_name=str(payload.get("job_name", "")),
                status=str(payload.get("status", RunStatus.RUNNING.value)),
                summary=str(payload.get("summary", "")),
                command_preview=str(payload.get("command_preview", "")),
                started_at=datetime.fromisoformat(payload["started_at"]) if payload.get("started_at") else datetime.now(UTC),
                updated_at=datetime.fromisoformat(payload["updated_at"]) if payload.get("updated_at") else datetime.now(UTC),
                bytes_transferred=int(payload.get("bytes_transferred", 0) or 0),
                total_bytes=int(payload.get("total_bytes", 0) or 0),
                files_transferred=int(payload.get("files_transferred", 0) or 0),
                total_files=int(payload.get("total_files", 0) or 0),
                speed=float(payload.get("speed", 0) or 0),
                eta_seconds=int(payload["eta_seconds"]) if payload.get("eta_seconds") is not None else None,
                listed=int(payload.get("listed", 0) or 0),
                checks=int(payload.get("checks", 0) or 0),
                total_checks=int(payload.get("total_checks", 0) or 0),
                active_items=list(payload.get("active_items", []) or []),
            )
        except (TypeError, ValueError):
            return None

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

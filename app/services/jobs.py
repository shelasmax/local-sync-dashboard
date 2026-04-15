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
from app.database import SessionLocal, migrate_sync_jobs_table
from app.models import AppSettings, ProfileType, RunHistory, RunStatus, StorageProfile, SyncJob
from app.schemas import SyncJobPayload
from app.services.common import decode_json, encode_json, is_temporary_network_error, relative_path_segments
from app.services.profiles import storage_endpoint
from app.services.rclone import (
    RcloneError,
    RcloneProgress,
    RcloneResult,
    RcloneSizeResult,
    build_copy_command,
    estimate_size,
    execute,
    execute_with_progress,
    is_available,
    summarize_rclone_error,
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
    existing = session.scalar(select(SyncJob).where(SyncJob.name == payload.name))
    if existing:
        raise JobConflictError(f"Задача с именем «{payload.name}» уже существует.")

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
        rclone_transfers=payload.rclone_transfers,
        rclone_checkers=payload.rclone_checkers,
        rclone_fast_list=payload.rclone_fast_list,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def update_job(session, job_id: int, payload: SyncJobPayload) -> SyncJob:
    job = session.get(SyncJob, job_id)
    if not job:
        raise LookupError("Задача не найдена.")

    existing = session.scalar(
        select(SyncJob).where(
            SyncJob.name == payload.name,
            SyncJob.id != job_id,
        )
    )
    if existing:
        raise JobConflictError(f"Задача с именем «{payload.name}» уже существует.")

    job.name = payload.name
    job.source_profile_id = payload.source_profile_id
    job.source_path = payload.source_path
    job.target_profile_id = payload.target_profile_id
    job.target_path = payload.target_path
    job.schedule = payload.schedule
    job.enabled = payload.enabled
    job.mode = payload.mode
    job.filters_json = encode_json(payload.filters)
    job.bandwidth_limit = payload.bandwidth_limit
    job.verify_checksum = payload.verify_checksum
    job.rclone_transfers = payload.rclone_transfers
    job.rclone_checkers = payload.rclone_checkers
    job.rclone_fast_list = payload.rclone_fast_list
    session.commit()
    session.refresh(job)
    return job


class JobDeleteError(RuntimeError):
    pass


class JobConflictError(ValueError):
    pass


class RunDeleteError(RuntimeError):
    pass


@dataclass(slots=True)
class RunCleanupResult:
    deleted_runs: int
    deleted_logs: int


@dataclass(slots=True)
class JobPreview:
    job_id: int
    job_name: str
    source: str
    target: str
    command_preview: str
    size_command_preview: str
    source_bytes: int
    source_files: int
    dry_run_result: RcloneResult


class RunRecoveryError(RuntimeError):
    pass


LOCAL_PROFILE_TYPES = {
    ProfileType.LOCAL_FOLDER.value,
    ProfileType.SYNOLOGY_SHARE.value,
}


def _display_relative_path(value: str) -> str:
    return value or "."


def _validate_synology_root(profile: StorageProfile) -> None:
    root = Path(profile.root_path_or_remote).expanduser()
    if root.parts[:1] != ("/",) or len(root.parts) < 2 or root.parts[1] != "Volumes":
        raise RcloneError(
            "Для Synology profile нужен уже смонтированный путь macOS из `/Volumes`, "
            f"а не `{profile.root_path_or_remote}`. Исправьте профиль-источник или профиль-назначение."
        )


def _validate_local_endpoint(
    profile: StorageProfile,
    *,
    relative_path: str,
    role: str,
    require_final_directory: bool,
) -> None:
    root = Path(profile.root_path_or_remote).expanduser()
    root_name = root.name.casefold()

    if profile.profile_type == ProfileType.SYNOLOGY_SHARE.value:
        _validate_synology_root(profile)

    try:
        segments = relative_path_segments(relative_path)
    except ValueError as exc:
        field = "source path" if role == "source" else "target path"
        raise RcloneError(f"Некорректный {field}: {exc}") from exc

    if segments and root_name and segments[0].casefold() == root_name:
        profile_role = "источника" if role == "source" else "назначения"
        field = "source_path" if role == "source" else "target_path"
        raise RcloneError(
            "Подпуть дублирует последнюю папку корня профиля. "
            f"У профиля {profile_role} уже выбран путь `{profile.root_path_or_remote}`. "
            f"Для этого случая оставьте `{field}` пустым. "
            "Если нужна папка уровнем выше, сначала измените корень профиля."
        )

    if not root.exists():
        role_label = "источник" if role == "source" else "назначение"
        prefix = "Локальный" if role == "source" else "Локальное"
        status_word = "недоступен" if role == "source" else "недоступно"
        raise RcloneError(
            f"{prefix} {role_label} {status_word}: корень профиля не найден: {root}. "
            "Проверьте profile root и что том смонтирован на этом Mac."
        )
    if not root.is_dir():
        role_label = "источник" if role == "source" else "назначение"
        prefix = "Локальный" if role == "source" else "Локальное"
        raise RcloneError(f"{prefix} {role_label} должно быть директорией: {root}")

    if not require_final_directory:
        return

    candidate = Path(storage_endpoint(profile, relative_path)).expanduser()
    if not candidate.exists():
        field = "source path" if role == "source" else "target path"
        if relative_path:
            raise RcloneError(
                f"Подпапка не найдена: `{_display_relative_path(relative_path)}`. "
                f"Проверьте {field} и корень профиля `{profile.root_path_or_remote}`."
            )
        raise RcloneError(
            f"Локальный путь не найден: {candidate}. "
            "Проверьте корень профиля и mount path на этом Mac."
        )
    if not candidate.is_dir():
        role_label = "источник" if role == "source" else "назначение"
        raise RcloneError(f"Локальное {role_label} должно быть директорией: {candidate}")


def validate_job_route(
    source_profile: StorageProfile,
    target_profile: StorageProfile,
    *,
    source_path: str = "",
    target_path: str = "",
) -> tuple[str, str]:
    if source_profile.profile_type in LOCAL_PROFILE_TYPES:
        _validate_local_endpoint(
            source_profile,
            relative_path=source_path,
            role="source",
            require_final_directory=False,
        )
    if target_profile.profile_type in LOCAL_PROFILE_TYPES:
        _validate_local_endpoint(
            target_profile,
            relative_path=target_path,
            role="target",
            require_final_directory=False,
        )
    return storage_endpoint(source_profile, source_path), storage_endpoint(target_profile, target_path)


def validate_job_endpoints(
    source_profile: StorageProfile,
    target_profile: StorageProfile,
    *,
    source_path: str = "",
    target_path: str = "",
) -> tuple[str, str]:
    source, target = validate_job_route(
        source_profile,
        target_profile,
        source_path=source_path,
        target_path=target_path,
    )

    if source_profile.profile_type in LOCAL_PROFILE_TYPES:
        _validate_local_endpoint(
            source_profile,
            relative_path=source_path,
            role="source",
            require_final_directory=True,
        )

    if target_profile.profile_type in LOCAL_PROFILE_TYPES:
        _validate_local_endpoint(
            target_profile,
            relative_path=target_path,
            role="target",
            require_final_directory=False,
        )

    return source, target


def delete_job(session, job_id: int, runner: "JobRunner") -> None:
    job = session.get(SyncJob, job_id)
    if not job:
        raise LookupError("Задача не найдена.")
    if job_id in runner.active_runs:
        raise JobDeleteError("Нельзя удалить активную задачу. Сначала остановите ее.")

    session.delete(job)
    session.commit()
    runner.sync_job(job_id)


def _delete_run_artifacts(run: RunHistory) -> int:
    if not run.log_path:
        return 0
    log_path = Path(run.log_path)
    if not log_path.exists():
        return 0
    log_path.unlink(missing_ok=True)
    return 1


def delete_run(session, run_id: int, runner: "JobRunner") -> None:
    run = session.get(RunHistory, run_id)
    if not run:
        raise LookupError("Запуск не найден.")
    if run.status == RunStatus.RUNNING.value:
        raise RunDeleteError("Нельзя удалить активный запуск. Сначала дождитесь завершения или остановите задачу.")
    if run.job_id in runner.active_runs:
        raise RunDeleteError("Нельзя удалить запуск активной задачи.")

    _delete_run_artifacts(run)
    session.delete(run)
    session.commit()


def cleanup_runs(
    session,
    runner: "JobRunner",
    *,
    status: str | None = None,
    older_than_days: int | None = None,
) -> RunCleanupResult:
    if older_than_days is None or older_than_days < 1:
        raise RunDeleteError("Для массовой очистки укажите возраст запусков в днях.")

    stmt = select(RunHistory).where(RunHistory.status != RunStatus.RUNNING.value)
    if status and status != "all":
        stmt = stmt.where(RunHistory.status == status)

    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    stmt = stmt.where(RunHistory.started_at < cutoff)
    runs = session.scalars(stmt).all()

    active_job_ids = set(runner.active_runs.keys())
    deleted_logs = 0
    deleted_runs = 0
    for run in runs:
        if run.job_id in active_job_ids:
            continue
        deleted_logs += _delete_run_artifacts(run)
        session.delete(run)
        deleted_runs += 1

    session.commit()
    return RunCleanupResult(deleted_runs=deleted_runs, deleted_logs=deleted_logs)


def retry_run(session, run_id: int, runner: "JobRunner") -> tuple[int, str]:
    run = session.get(RunHistory, run_id)
    if not run:
        raise LookupError("Запуск не найден.")
    if run.status != RunStatus.INTERRUPTED.value:
        raise RunRecoveryError("Повтор через recovery доступен только для прерванных запусков.")

    job = session.get(SyncJob, run.job_id)
    if not job:
        raise RunRecoveryError("У этого запуска больше нет связанной задачи.")

    ok, message = runner.enqueue_job(job.id, initiated_by="manual")
    if not ok:
        raise RunRecoveryError(message)
    return job.id, message


def preview_job(session, job_id: int, timeout_seconds: int = 60) -> JobPreview:
    job = session.get(SyncJob, job_id)
    if not job:
        raise LookupError("Задача не найдена.")

    source_profile = session.get(StorageProfile, job.source_profile_id)
    target_profile = session.get(StorageProfile, job.target_profile_id)
    if not source_profile or not target_profile:
        raise LookupError("Не найден профиль источника или назначения.")

    source, target = validate_job_endpoints(
        source_profile,
        target_profile,
        source_path=job.source_path,
        target_path=job.target_path,
    )
    filters = decode_json(job.filters_json, [])
    size_result: RcloneSizeResult = estimate_size(source, filters=filters, timeout_seconds=timeout_seconds)
    dry_run_command = build_copy_command(
        source=source,
        target=target,
        filters=filters,
        bandwidth_limit=job.bandwidth_limit,
        verify_checksum=job.verify_checksum,
        transfers=job.rclone_transfers,
        checkers=job.rclone_checkers,
        fast_list=job.rclone_fast_list,
        dry_run=True,
    )
    dry_run_result = execute(dry_run_command, timeout_seconds=timeout_seconds)
    return JobPreview(
        job_id=job.id,
        job_name=job.name,
        source=source,
        target=target,
        command_preview=dry_run_result.command_preview,
        size_command_preview=size_result.command_preview,
        source_bytes=size_result.bytes_total,
        source_files=size_result.files_total,
        dry_run_result=dry_run_result,
    )


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
        self.scheduler.add_job(
            self._reap_orphan_runs,
            "interval",
            minutes=5,
            id="orphan-run-reaper",
            replace_existing=True,
        )

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
        migrate_sync_jobs_table()
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
        run_id: int | None = None
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

                try:
                    source, target = validate_job_endpoints(
                        source_profile,
                        target_profile,
                        source_path=job.source_path,
                        target_path=job.target_path,
                    )
                except RcloneError as exc:
                    self._create_blocked_run(job_id, str(exc))
                    return
                filters = decode_json(job.filters_json, [])
                command = build_copy_command(
                    source=source,
                    target=target,
                    filters=filters,
                    bandwidth_limit=job.bandwidth_limit,
                    verify_checksum=job.verify_checksum,
                    transfers=job.rclone_transfers,
                    checkers=job.rclone_checkers,
                    fast_list=job.rclone_fast_list,
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
                run_id = run.id

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
                        self._finalize_snapshot(job_id, run.status, run.summary, 0, 0)
                        return
                    except Exception as exc:
                        result = None
                        run.status = RunStatus.FAILED.value
                        run.summary = f"Неожиданная ошибка: {exc}"
                        run.stderr = str(exc)
                        run.finished_at = datetime.now(UTC)
                        session.commit()
                        self._finalize_snapshot(job_id, run.status, run.summary, 0, 0)
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
                    run.summary = summarize_rclone_error(combined, returncode=result.returncode)
                session.commit()
                self._finalize_snapshot(job_id, run.status, run.summary, result.bytes_transferred, result.files_transferred)
        except Exception:
            if run_id is not None:
                try:
                    self._mark_run_failed(run_id)
                except Exception:
                    pass
        finally:
            with self.lock:
                self.active_runs.pop(job_id, None)
            self.cleanup_old_runs()

    def _mark_run_failed(self, run_id: int) -> None:
        with SessionLocal() as session:
            run = session.get(RunHistory, run_id)
            if not run or run.status != RunStatus.RUNNING.value:
                return
            run.status = RunStatus.FAILED.value
            run.summary = "Неожиданная ошибка во время выполнения."
            run.finished_at = datetime.now(UTC)
            session.commit()

    def cleanup_old_runs(self) -> None:
        with SessionLocal() as session:
            app_settings = init_settings(session)
            cutoff = datetime.now(UTC) - timedelta(days=app_settings.log_retention_days)
            expired_runs = session.scalars(select(RunHistory).where(RunHistory.started_at < cutoff)).all()
            for run in expired_runs:
                _delete_run_artifacts(run)
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
            run.summary = (
                f"Запуск был прерван. Последний известный прогресс: "
                f"{snapshot.files_transferred}/{snapshot.total_files or '?'} файлов, "
                f"{snapshot.bytes_transferred}/{snapshot.total_bytes or '?'} байт."
            )
            session.commit()

    def _reap_orphan_runs(self) -> None:
        """Periodic check: mark DB runs as INTERRUPTED if no active thread owns them."""
        with self.lock:
            active_job_ids = set(self.active_runs.keys())

        with SessionLocal() as session:
            orphan_runs = session.scalars(
                select(RunHistory).where(
                    RunHistory.status == RunStatus.RUNNING.value,
                )
            ).all()
            reaped = False
            for run in orphan_runs:
                if run.job_id in active_job_ids:
                    continue
                run.status = RunStatus.INTERRUPTED.value
                run.summary = "Задача потеряна: процесс rclone завершился, но статус не был обновлен."
                run.finished_at = datetime.now(UTC)
                reaped = True
            if reaped:
                session.commit()

        settings.ensure_runtime_dirs()
        for snapshot_file in settings.runtime_dir.glob("run-*.json"):
            snapshot = self._load_snapshot(snapshot_file)
            if snapshot and snapshot.job_id not in active_job_ids:
                snapshot_file.unlink(missing_ok=True)

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

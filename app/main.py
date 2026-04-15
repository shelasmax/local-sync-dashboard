from __future__ import annotations

from contextlib import asynccontextmanager
import json
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import Base, SessionLocal, engine, get_session
from app.models import ProfileType, RunHistory, StorageProfile, SyncJob
from app.schemas import (
    ConnectionTestPayload,
    RunHistoryRead,
    StorageProfilePayload,
    StorageProfileRead,
    SyncJobPayload,
    SyncJobRead,
)
from app.services.common import decode_json
from app.services.jobs import (
    JobConflictError,
    JobDeleteError,
    RunRecoveryError,
    create_job,
    delete_job,
    init_settings,
    job_runner,
    preview_job,
    retry_run,
    update_job,
    validate_job_route,
)
from app.services.rclone import RcloneError
from app.services.profiles import (
    ProfileBrowseResult,
    ProfileCheckResult,
    ProfileConflictError,
    ProfileInUseError,
    browse_profile,
    build_s3_remote_root,
    check_profile,
    create_profile,
    delete_profile,
    deserialize_profile_options,
    diagnostics_from_options,
    split_s3_remote_root,
    test_connection,
    update_profile,
    user_profile_options,
)
from app.services.scheduling import describe_schedule
from app.services.system import collect_setup_diagnostics

PROFILE_TYPE_LABELS = {
    ProfileType.LOCAL_FOLDER.value: "Локальная папка",
    ProfileType.SYNOLOGY_SHARE.value: "Смонтированная папка Synology",
    ProfileType.S3_REMOTE.value: "S3 remote",
    ProfileType.YANDEX_REMOTE.value: "Яндекс Диск remote",
}

STATUS_LABELS = {
    "running": "выполняется",
    "success": "успешно",
    "failed": "ошибка",
    "canceled": "отменено",
    "blocked": "заблокировано",
    "interrupted": "прервано",
    "ready": "готов",
    "unreachable": "недоступен",
    "unknown": "неизвестно",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_runtime_dirs()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        init_settings(session)
    job_runner.start()
    try:
        yield
    finally:
        job_runner.shutdown()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
templates = Jinja2Templates(directory=str(settings.templates_dir))


def profile_type_label(value: str) -> str:
    return PROFILE_TYPE_LABELS.get(value, value)


def status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value)


def format_bytes(value: int | float | None) -> str:
    if value in (None, 0):
        return "0 B"
    amount = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit_index = 0
    while amount >= 1024 and unit_index < len(units) - 1:
        amount /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(amount)} {units[unit_index]}"
    return f"{amount:.1f} {units[unit_index]}"


def format_speed(value: int | float | None) -> str:
    if not value or not isfinite(float(value)):
        return "0 B/s"
    return f"{format_bytes(float(value))}/s"


def format_eta(seconds: int | None) -> str:
    if seconds is None:
        return "ETA неизвестно"
    if seconds <= 0:
        return "меньше минуты"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes} мин")
    return " ".join(parts) if parts else "меньше минуты"


def profile_connection_hint(profile: StorageProfile, configured_remotes: set[str]) -> str:
    if profile.profile_type in {ProfileType.LOCAL_FOLDER.value, ProfileType.SYNOLOGY_SHARE.value}:
        candidate = Path(profile.root_path_or_remote).expanduser()
        if not candidate.exists():
            return f"Путь не найден: {candidate}"
        if not candidate.is_dir():
            return f"Путь не является директорией: {candidate}"
        if profile.status == "ready":
            return "Локальный путь найден и последняя проверка прошла."
        return "Путь выглядит доступным, но последняя проверка завершилась ошибкой. Нажмите «Проверить снова»."

    remote_name, _, _ = split_s3_remote_root(profile.root_path_or_remote)
    if not remote_name:
        return "У профиля не указан `rclone remote`."
    if remote_name in configured_remotes:
        if profile.profile_type == ProfileType.S3_REMOTE.value:
            options = deserialize_profile_options(profile)
            endpoint = str(options.get("endpoint", "")).strip()
            region = str(options.get("region", "")).strip()
            meta = [part for part in [f"endpoint: {endpoint}" if endpoint else "", f"region: {region}" if region else ""] if part]
            if profile.status == "ready":
                suffix = f" ({', '.join(meta)})" if meta else ""
                return f"Remote `{remote_name}:` найден и последняя проверка прошла.{suffix}"
            suffix = f" Проверьте настройки: {', '.join(meta)}." if meta else ""
            return f"Remote `{remote_name}:` найден, но последняя проверка завершилась ошибкой.{suffix}"
        if profile.status == "ready":
            return f"Remote `{remote_name}:` найден и последняя проверка прошла."
        return f"Remote `{remote_name}:` найден, но последняя проверка завершилась ошибкой. Нажмите «Проверить снова»."

    known = ", ".join(f"{item}:" for item in sorted(configured_remotes)) or "нет настроенных remote"
    return f"Remote `{remote_name}:` не найден в `rclone`. Сейчас доступны: {known}."


def is_remote_profile(profile: StorageProfile | None) -> bool:
    if not profile:
        return False
    return profile.profile_type in {ProfileType.S3_REMOTE.value, ProfileType.YANDEX_REMOTE.value}


def job_transfer_warning(
    source_profile: StorageProfile | None,
    target_profile: StorageProfile | None,
) -> str:
    if is_remote_profile(source_profile) and is_remote_profile(target_profile):
        return "Cloud-to-cloud маршрут идет через локальный Mac: `rclone` читает source и затем отправляет данные в target. Скорость зависит от интернета, sleep и перезагрузок этого компьютера."
    if is_remote_profile(source_profile) or is_remote_profile(target_profile):
        return "Маршрут использует cloud remote, но перенос все равно выполняется локально на этом Mac через `rclone`."
    return ""


def job_pre_run_checklist(
    source_profile: StorageProfile | None,
    target_profile: StorageProfile | None,
) -> list[str]:
    if not (is_remote_profile(source_profile) or is_remote_profile(target_profile)):
        return []

    items = [
        "Запускайте длинные выгрузки тогда, когда Mac не уйдет в sleep.",
        "Для больших cloud задач лучше использовать ночное окно или другой длинный стабильный интервал.",
        "Проверьте, что интернет стабилен и на Mac достаточно системных ресурсов.",
    ]
    if is_remote_profile(source_profile) and is_remote_profile(target_profile):
        items.append("Маршрут идет через локальный Mac целиком: объем загрузки и выгрузки проходит через этот компьютер.")
    return items


def interrupted_run_recovery_steps(run: RunHistory) -> list[str]:
    steps = [
        "Проверьте причину прерывания: sleep, пропавшая сеть, недоступный remote или отвалившийся mount path.",
        "Ориентируйтесь на последний известный прогресс в этом запуске: уже переданные файлы повторно удаляться не будут.",
    ]
    if run.job and (is_remote_profile(run.job.source_profile) or is_remote_profile(run.job.target_profile)):
        steps.append("Для длинного повторного запуска выберите окно, в котором Mac не уснет и интернет будет стабильным.")
    steps.append("После исправления причины используйте «Повторить с дельты» или повторный ручной запуск задачи.")
    return steps


def job_requires_manual_start_checklist(job: SyncJob | None) -> bool:
    if not job:
        return False
    return is_remote_profile(job.source_profile) or is_remote_profile(job.target_profile)


def suggested_profiles(diagnostics) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    for path in diagnostics.local_candidates["icloud"]:
        suggestions.append(
            {
                "name": "iCloud Drive",
                "profile_type": ProfileType.LOCAL_FOLDER.value,
                "path": path,
                "description": "Локальная папка iCloud Drive на этом Mac.",
            }
        )
    for path in diagnostics.local_candidates["google_drive"]:
        suggestions.append(
            {
                "name": "Google Drive",
                "profile_type": ProfileType.LOCAL_FOLDER.value,
                "path": path,
                "description": "Локальная папка Google Drive for Desktop.",
            }
        )
    for path in diagnostics.local_candidates["mounted_volumes"]:
        suggestions.append(
            {
                "name": Path(path).name,
                "profile_type": ProfileType.LOCAL_FOLDER.value,
                "path": path,
                "description": "Смонтированный том или NAS-шара из /Volumes. При необходимости можно считать это источником с Synology.",
            }
        )
    for remote in diagnostics.remotes:
        remote_type = ProfileType.YANDEX_REMOTE.value if "yandex" in remote.lower() or "ya" in remote.lower() else ProfileType.S3_REMOTE.value
        suggestions.append(
            {
                "name": remote,
                "profile_type": remote_type,
                "path": f"{remote}:",
                "description": "Remote-подключение rclone, уже готовое для профиля.",
            }
        )
    return suggestions


def prefill_job_payload(params: dict[str, Any] | None = None) -> dict[str, Any]:
    data = params or {}
    return {
        "form_mode": data.get("form_mode", "create"),
        "edit_job_id": int(data["edit_job_id"]) if data.get("edit_job_id") else None,
        "clone_job_id": int(data["clone_job_id"]) if data.get("clone_job_id") else None,
        "name": data.get("suggested_name") or data.get("name", ""),
        "source_profile_id": int(data["source_profile_id"]) if data.get("source_profile_id") else None,
        "source_path": data.get("source_path", ""),
        "target_profile_id": int(data["target_profile_id"]) if data.get("target_profile_id") else None,
        "target_path": data.get("target_path", ""),
        "schedule": data.get("schedule", "manual"),
        "bandwidth_limit": data.get("bandwidth_limit", ""),
        "filters": data.get("filters", ""),
        "enabled": str(data.get("enabled", "true")).lower() not in {"false", "0", ""},
        "verify_checksum": str(data.get("verify_checksum", "false")).lower() in {"true", "1", "on"},
        "error_field": data.get("error_field", ""),
    }


def build_job_form_query(
    *,
    error: str,
    error_field: str = "",
    name: str,
    source_profile_id: int,
    source_path: str,
    target_profile_id: int,
    target_path: str,
    schedule: str,
    bandwidth_limit: str,
    verify_checksum: bool,
    filters: str,
    enabled: bool,
    form_mode: str = "create",
    edit_job_id: int | None = None,
    clone_job_id: int | None = None,
) -> str:
    query = {
        "error": error,
        "error_field": error_field,
        "form_mode": form_mode,
        "name": name,
        "source_profile_id": source_profile_id,
        "source_path": source_path,
        "target_profile_id": target_profile_id,
        "target_path": target_path,
        "schedule": schedule,
        "bandwidth_limit": bandwidth_limit,
        "verify_checksum": "true" if verify_checksum else "false",
        "filters": filters,
        "enabled": "true" if enabled else "false",
    }
    if edit_job_id is not None:
        query["edit_job_id"] = edit_job_id
    if clone_job_id is not None:
        query["clone_job_id"] = clone_job_id
    return urlencode(query)


def build_job_prefill(job: SyncJob, *, mode: str) -> dict[str, Any]:
    name = job.name
    if mode == "clone":
        name = f"{job.name} (copy)"
    return {
        "form_mode": mode,
        "edit_job_id": job.id if mode == "edit" else None,
        "clone_job_id": job.id if mode == "clone" else None,
        "name": name,
        "source_profile_id": job.source_profile_id,
        "source_path": job.source_path,
        "target_profile_id": job.target_profile_id,
        "target_path": job.target_path,
        "schedule": job.schedule,
        "bandwidth_limit": job.bandwidth_limit or "",
        "filters": "\n".join(decode_json(job.filters_json, [])),
        "enabled": job.enabled,
        "verify_checksum": job.verify_checksum,
        "error_field": "",
    }


def job_form_context(prefill: dict[str, Any]) -> dict[str, str]:
    mode = prefill.get("form_mode", "create")
    if mode == "edit":
        return {
            "mode": "edit",
            "title": "Редактирование задачи",
            "note": "Вы правите существующий маршрут. Сохранение обновит расписание и следующие ручные запуски.",
            "banner": "Исправьте маршрут и сохраните задачу перед новым запуском.",
            "submit_label": "Сохранить изменения",
            "submit_title": "Сохранить изменения задачи",
        }
    if mode == "clone":
        return {
            "mode": "clone",
            "title": "Новая задача на основе текущей",
            "note": "Используйте этот режим, чтобы быстро создать безопасную копию маршрута после ошибки или для эксперимента.",
            "banner": "Проверьте имя, source/target и сохраните новую задачу отдельно от исходной.",
            "submit_label": "Создать копию",
            "submit_title": "Создать копию задачи",
        }
    return {
        "mode": "create",
        "title": "Создать задачу синхронизации",
        "note": "Опциональный блок. Открывайте его, когда нужно добавить новый маршрут `copy`.",
        "banner": "",
        "submit_label": "Сохранить задачу",
        "submit_title": "Сохранить задачу",
    }


def resolve_job_profiles(
    session: Session,
    *,
    source_profile_id: int,
    target_profile_id: int,
) -> tuple[StorageProfile, StorageProfile]:
    source_profile = session.get(StorageProfile, source_profile_id)
    target_profile = session.get(StorageProfile, target_profile_id)
    if not source_profile or not target_profile:
        raise LookupError("Не найден профиль источника или назначения.")
    return source_profile, target_profile


def route_preview_payload(
    source_profile: StorageProfile | None,
    target_profile: StorageProfile | None,
    *,
    source_path: str = "",
    target_path: str = "",
) -> dict[str, Any]:
    payload = {
        "source_root": source_profile.root_path_or_remote if source_profile else "",
        "source_path": source_path,
        "source_effective": "",
        "target_root": target_profile.root_path_or_remote if target_profile else "",
        "target_path": target_path,
        "target_effective": "",
        "warnings": [],
    }
    if not source_profile or not target_profile:
        return payload
    try:
        source_effective, target_effective = validate_job_route(
            source_profile,
            target_profile,
            source_path=source_path,
            target_path=target_path,
        )
        payload["source_effective"] = source_effective
        payload["target_effective"] = target_effective
    except RcloneError as exc:
        payload["warnings"] = [str(exc)]
    return payload


def latest_problem_runs_by_job(runs: list[RunHistory]) -> dict[int, dict[str, Any]]:
    incidents: dict[int, dict[str, Any]] = {}
    streaks: dict[tuple[int, str], int] = {}
    for run in runs:
        if run.job_id is None:
            continue
        if run.status in {"failed", "blocked", "interrupted"}:
            key = (run.job_id, run.summary)
            streaks[key] = streaks.get(key, 0) + 1
            if run.job_id not in incidents:
                incidents[run.job_id] = {
                    "run": run,
                    "streak": streaks[key],
                    "summary": run.summary,
                }
        elif run.job_id not in incidents:
            incidents[run.job_id] = {
                "run": None,
                "streak": 0,
                "summary": "",
            }
    return {job_id: item for job_id, item in incidents.items() if item["run"] is not None}


def build_profile_prefill(
    *,
    suggested_name: str = "",
    suggested_type: str = "",
    suggested_root: str = "",
    suggested_options_json: str = "{}",
    edit_profile_id: int | None = None,
) -> dict[str, Any]:
    options = user_profile_options(decode_json(suggested_options_json, {}))
    s3_remote_name, s3_bucket, s3_prefix = split_s3_remote_root(suggested_root)
    return {
        "id": edit_profile_id,
        "name": suggested_name,
        "profile_type": suggested_type,
        "root_path_or_remote": suggested_root,
        "options_json": suggested_options_json,
        "s3_remote_name": s3_remote_name,
        "s3_bucket": s3_bucket,
        "s3_prefix": s3_prefix,
        "s3_provider": str(options.get("provider", "")),
        "s3_region": str(options.get("region", "")),
        "s3_endpoint": str(options.get("endpoint", "")),
    }


def profile_last_check(profile: StorageProfile) -> dict[str, Any]:
    return diagnostics_from_options(deserialize_profile_options(profile))


def profile_diagnostic_hints(profile: StorageProfile, configured_remotes: set[str]) -> list[str]:
    hints: list[str] = []
    last_check = profile_last_check(profile)
    message = str(last_check.get("message", "")).lower()
    options = deserialize_profile_options(profile)

    if profile.profile_type in {ProfileType.LOCAL_FOLDER.value, ProfileType.SYNOLOGY_SHARE.value}:
        candidate = Path(profile.root_path_or_remote).expanduser()
        if not candidate.exists():
            hints.append("Проверьте, что путь существует и том или папка действительно смонтированы на этом Mac.")
        elif not candidate.is_dir():
            hints.append("Укажите именно директорию, а не отдельный файл.")
        elif "permission" in message or "access" in message:
            hints.append("Проверьте права доступа к каталогу для текущего пользователя macOS.")
        elif profile.status != "ready":
            hints.append("Если это NAS-шара, убедитесь, что она не отвалилась из `/Volumes` после sleep или переподключения сети.")
        return hints

    remote_name, _, bucket = split_s3_remote_root(profile.root_path_or_remote)
    if remote_name and remote_name not in configured_remotes:
        hints.append(f"Сначала создайте или исправьте `rclone remote` `{remote_name}:` через `rclone config`.")

    if "unauthorized" in message or "401" in message or "403" in message or "access denied" in message:
        if profile.profile_type == ProfileType.YANDEX_REMOTE.value:
            hints.append("Похоже на проблему авторизации Яндекс Диска: переподключите remote через `rclone config reconnect` или заново выполните OAuth.")
        else:
            hints.append("Похоже на проблему доступа к S3: проверьте `access_key`, `secret_key`, права на bucket и политику доступа.")

    if "timeout" in message or "tls" in message or "connection" in message or "no such host" in message:
        hints.append("Похоже на сетевую ошибку: проверьте интернет, DNS, VPN и доступность endpoint.")

    if profile.profile_type == ProfileType.S3_REMOTE.value:
        endpoint = str(options.get("endpoint", "")).strip()
        region = str(options.get("region", "")).strip()
        if not endpoint:
            hints.append("Для S3-compatible хранилищ обычно нужен явный `endpoint` в настройках профиля.")
        if not region:
            hints.append("Если провайдер требует регион, заполните поле `region`, даже если bucket уже указан.")
        if "bucket" in message or "not found" in message:
            hints.append(f"Проверьте имя bucket и prefix в remote-пути. Сейчас указан bucket: `{bucket or 'не задан'}`.")
    elif profile.profile_type == ProfileType.YANDEX_REMOTE.value and profile.status != "ready":
        hints.append("Для Яндекс Диска обычно достаточно одного рабочего remote; если remote найден, чаще всего помогает переподключение OAuth.")

    unique_hints: list[str] = []
    for hint in hints:
        if hint not in unique_hints:
            unique_hints.append(hint)
    return unique_hints


def profile_edit_url(profile: StorageProfile) -> str:
    query = urlencode(
        {
            "edit_profile_id": profile.id,
            "suggested_name": profile.name,
            "suggested_type": profile.profile_type,
            "suggested_root": profile.root_path_or_remote,
            "suggested_options_json": json.dumps(
                user_profile_options(deserialize_profile_options(profile)),
                ensure_ascii=False,
                indent=2,
            ),
        }
    )
    return f"/profiles?{query}"


def profile_diagnostic_actions(profile: StorageProfile, configured_remotes: set[str]) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    last_check = profile_last_check(profile)
    message = str(last_check.get("message", "")).lower()

    def add_link(label: str, href: str) -> None:
        item = {"kind": "link", "label": label, "href": href}
        if item not in actions:
            actions.append(item)

    def add_check(label: str = "Проверить снова") -> None:
        item = {"kind": "check", "label": label}
        if item not in actions:
            actions.append(item)

    edit_url = profile_edit_url(profile)

    if profile.profile_type in {ProfileType.LOCAL_FOLDER.value, ProfileType.SYNOLOGY_SHARE.value}:
        candidate = Path(profile.root_path_or_remote).expanduser()
        if not candidate.exists() or not candidate.is_dir():
            add_link("Исправить профиль", edit_url)
            add_link("Открыть setup", "/setup")
            return actions
        if "permission" in message or "access" in message:
            add_check()
            add_link("Исправить профиль", edit_url)
            return actions
        if profile.status != "ready":
            add_check()
            add_link("Открыть setup", "/setup")
        return actions

    remote_name, _, _ = split_s3_remote_root(profile.root_path_or_remote)
    if remote_name and remote_name not in configured_remotes:
        add_link("Открыть setup", "/setup")
        add_link("Исправить профиль", edit_url)
        return actions

    if "unauthorized" in message or "401" in message or "403" in message or "access denied" in message:
        add_link("Открыть setup", "/setup")
        add_link("Исправить профиль", edit_url)
        add_check("Проверить после исправления")
        return actions

    if "timeout" in message or "tls" in message or "connection" in message or "no such host" in message:
        add_check()
        add_link("Открыть setup", "/setup")
        return actions

    if profile.profile_type == ProfileType.S3_REMOTE.value:
        add_link("Исправить профиль", edit_url)
        if profile.status != "ready":
            add_check()
        return actions

    if profile.profile_type == ProfileType.YANDEX_REMOTE.value and profile.status != "ready":
        add_link("Открыть setup", "/setup")
        add_check()
        return actions

    return actions


def normalize_profile_form_inputs(
    *,
    profile_type: str,
    root_path_or_remote: str,
    options_json: str,
    s3_remote_name: str,
    s3_bucket: str,
    s3_prefix: str,
    s3_provider: str,
    s3_region: str,
    s3_endpoint: str,
) -> tuple[str, dict[str, Any]]:
    options = decode_json(options_json, {})
    if profile_type != ProfileType.S3_REMOTE.value:
        return root_path_or_remote, options

    normalized_root = build_s3_remote_root(
        s3_remote_name or split_s3_remote_root(root_path_or_remote)[0],
        s3_bucket,
        s3_prefix,
    )
    merged_options = dict(options)
    s3_fields = {
        "provider": s3_provider.strip(),
        "region": s3_region.strip(),
        "endpoint": s3_endpoint.strip(),
    }
    for key, value in s3_fields.items():
        if value:
            merged_options[key] = value
        else:
            merged_options.pop(key, None)
    return normalized_root, merged_options


def suggested_job_templates(profiles: list[StorageProfile]) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    indexed = {profile.id: profile for profile in profiles}

    def add_template(
        title: str,
        description: str,
        source_id: int,
        target_id: int,
        source_path: str = "",
        target_path: str = "",
        schedule: str = "manual",
    ) -> None:
        source = indexed[source_id]
        target = indexed[target_id]
        templates.append(
            {
                "title": title,
                "description": description,
                "source_name": source.name,
                "target_name": target.name,
                "params": {
                    "suggested_name": title,
                    "source_profile_id": source_id,
                    "source_path": source_path,
                    "target_profile_id": target_id,
                    "target_path": target_path,
                    "schedule": schedule,
                    "enabled": "true",
                    "verify_checksum": "false",
                    "filters": "",
                },
            }
        )

    local_sources = [
        profile
        for profile in profiles
        if profile.profile_type in {ProfileType.LOCAL_FOLDER.value, ProfileType.SYNOLOGY_SHARE.value}
    ]
    remote_targets = [
        profile
        for profile in profiles
        if profile.profile_type in {ProfileType.YANDEX_REMOTE.value, ProfileType.S3_REMOTE.value}
    ]
    remote_sources = remote_targets
    local_targets = local_sources

    for source in local_sources:
        for target in remote_targets:
            if source.id == target.id:
                continue
            add_template(
                title=f"{source.name} -> {target.name}",
                description="Выгрузка локальных данных или смонтированной NAS-шары в облачное хранилище.",
                source_id=source.id,
                target_id=target.id,
                schedule="daily",
            )

    for source in remote_sources:
        for target in local_targets:
            if source.id == target.id:
                continue
            add_template(
                title=f"{source.name} -> {target.name}",
                description="Возвратная выгрузка из облака в локальную папку или на смонтированный диск.",
                source_id=source.id,
                target_id=target.id,
                schedule="manual",
            )

    unique_templates: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in templates:
        key = (int(item["params"]["source_profile_id"]), int(item["params"]["target_profile_id"]))
        if key in seen:
            continue
        seen.add(key)
        unique_templates.append(item)
    return unique_templates[:12]


def _list_profiles(session: Session) -> list[StorageProfile]:
    return session.scalars(select(StorageProfile).order_by(StorageProfile.name)).all()


def _list_jobs(session: Session) -> list[SyncJob]:
    return session.scalars(
        select(SyncJob)
        .options(selectinload(SyncJob.source_profile), selectinload(SyncJob.target_profile))
        .order_by(SyncJob.name)
    ).all()


def _list_runs(session: Session, limit: int = 50) -> list[RunHistory]:
    return session.scalars(
        select(RunHistory)
        .options(
            selectinload(RunHistory.job).selectinload(SyncJob.source_profile),
            selectinload(RunHistory.job).selectinload(SyncJob.target_profile),
        )
        .order_by(desc(RunHistory.started_at))
        .limit(limit)
    ).all()


def _list_interrupted_runs(session: Session, limit: int = 5) -> list[RunHistory]:
    return session.scalars(
        select(RunHistory)
        .options(
            selectinload(RunHistory.job).selectinload(SyncJob.source_profile),
            selectinload(RunHistory.job).selectinload(SyncJob.target_profile),
        )
        .where(RunHistory.status == "interrupted")
        .order_by(desc(RunHistory.started_at))
        .limit(limit)
    ).all()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    stats = job_runner.status()
    recent_runs = _list_runs(session, limit=10)
    interrupted_runs = _list_interrupted_runs(session, limit=3)
    diagnostics = collect_setup_diagnostics()
    incidents_by_job = latest_problem_runs_by_job(_list_runs(session, limit=50))
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "stats": stats,
            "recent_runs": recent_runs,
            "interrupted_runs": interrupted_runs,
            "diagnostics": diagnostics,
            "status_label": status_label,
            "format_bytes": format_bytes,
            "format_speed": format_speed,
            "format_eta": format_eta,
            "interrupted_run_recovery_steps": interrupted_run_recovery_steps,
            "job_requires_manual_start_checklist": job_requires_manual_start_checklist,
            "incidents_by_job": incidents_by_job,
        },
    )


@app.get("/health")
def health():
    return job_runner.status()


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, session: Session = Depends(get_session)):
    diagnostics = collect_setup_diagnostics()
    profiles = _list_profiles(session)
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={
            "request": request,
            "diagnostics": diagnostics,
            "suggestions": suggested_profiles(diagnostics),
            "job_templates": suggested_job_templates(profiles),
            "profiles_count": len(profiles),
            "profile_type_label": profile_type_label,
        },
    )


@app.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="ops.html",
        context={
            "request": request,
            "launchd_plist_path": "launchd/com.localsync.dashboard.plist",
            "data_dir": str(settings.data_dir),
            "db_path": str(settings.db_path),
            "logs_dir": str(settings.logs_dir),
            "runtime_dir": str(settings.runtime_dir),
        },
    )


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page(
    request: Request,
    suggested_name: str = "",
    suggested_type: str = "",
    suggested_root: str = "",
    suggested_options_json: str = "{}",
    edit_profile_id: int | None = None,
    error: str = "",
    info: str = "",
    checked_profile_id: int | None = None,
    session: Session = Depends(get_session),
):
    profiles = _list_profiles(session)
    configured_remotes = set(collect_setup_diagnostics().remotes)
    return templates.TemplateResponse(
        request=request,
        name="profiles.html",
        context={
            "request": request,
            "profiles": profiles,
            "profile_types": [item.value for item in ProfileType],
            "profile_type_label": profile_type_label,
            "status_label": status_label,
            "profile_connection_hint": profile_connection_hint,
            "profile_last_check": profile_last_check,
            "profile_diagnostic_hints": profile_diagnostic_hints,
            "profile_diagnostic_actions": profile_diagnostic_actions,
            "configured_remotes": configured_remotes,
            "prefill": build_profile_prefill(
                suggested_name=suggested_name,
                suggested_type=suggested_type,
                suggested_root=suggested_root,
                suggested_options_json=suggested_options_json,
                edit_profile_id=edit_profile_id,
            ),
            "error": error,
            "info": info,
            "checked_profile_id": checked_profile_id,
        },
    )


@app.post("/profiles")
def create_profile_html(
    name: str = Form(...),
    profile_type: str = Form(...),
    root_path_or_remote: str = Form(...),
    secret: str = Form(default=""),
    options_json: str = Form(default="{}"),
    s3_remote_name: str = Form(default=""),
    s3_bucket: str = Form(default=""),
    s3_prefix: str = Form(default=""),
    s3_provider: str = Form(default=""),
    s3_region: str = Form(default=""),
    s3_endpoint: str = Form(default=""),
    session: Session = Depends(get_session),
):
    normalized_root, normalized_options = normalize_profile_form_inputs(
        profile_type=profile_type,
        root_path_or_remote=root_path_or_remote,
        options_json=options_json,
        s3_remote_name=s3_remote_name,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        s3_provider=s3_provider,
        s3_region=s3_region,
        s3_endpoint=s3_endpoint,
    )
    payload = StorageProfilePayload(
        name=name,
        profile_type=profile_type,
        root_path_or_remote=normalized_root,
        secret=secret or None,
        options=normalized_options,
    )
    try:
        create_profile(session, payload)
    except ProfileConflictError as exc:
        query = urlencode(
            {
                "error": str(exc),
                "edit_profile_id": "",
                "suggested_name": name,
                "suggested_type": profile_type,
                "suggested_root": normalized_root,
                "suggested_options_json": json.dumps(normalized_options, ensure_ascii=False, indent=2),
            }
        )
        return RedirectResponse(url=f"/profiles?{query}", status_code=303)
    return RedirectResponse(url="/profiles", status_code=303)


@app.post("/profiles/{profile_id}/update")
def update_profile_html(
    profile_id: int,
    name: str = Form(...),
    profile_type: str = Form(...),
    root_path_or_remote: str = Form(...),
    secret: str = Form(default=""),
    clear_secret: bool = Form(default=False),
    options_json: str = Form(default="{}"),
    s3_remote_name: str = Form(default=""),
    s3_bucket: str = Form(default=""),
    s3_prefix: str = Form(default=""),
    s3_provider: str = Form(default=""),
    s3_region: str = Form(default=""),
    s3_endpoint: str = Form(default=""),
    session: Session = Depends(get_session),
):
    normalized_root, normalized_options = normalize_profile_form_inputs(
        profile_type=profile_type,
        root_path_or_remote=root_path_or_remote,
        options_json=options_json,
        s3_remote_name=s3_remote_name,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        s3_provider=s3_provider,
        s3_region=s3_region,
        s3_endpoint=s3_endpoint,
    )
    payload = StorageProfilePayload(
        name=name,
        profile_type=profile_type,
        root_path_or_remote=normalized_root,
        secret=secret or None,
        options=normalized_options,
    )
    try:
        update_profile(session, profile_id, payload, clear_secret=clear_secret)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProfileConflictError as exc:
        query = urlencode(
            {
                "error": str(exc),
                "edit_profile_id": profile_id,
                "suggested_name": name,
                "suggested_type": profile_type,
                "suggested_root": normalized_root,
                "suggested_options_json": json.dumps(normalized_options, ensure_ascii=False, indent=2),
            }
        )
        return RedirectResponse(url=f"/profiles?{query}", status_code=303)
    return RedirectResponse(url="/profiles", status_code=303)


@app.post("/profiles/{profile_id}/delete")
def delete_profile_html(profile_id: int, session: Session = Depends(get_session)):
    try:
        delete_profile(session, profile_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProfileInUseError as exc:
        query = urlencode({"error": str(exc)})
        return RedirectResponse(url=f"/profiles?{query}", status_code=303)
    return RedirectResponse(url="/profiles", status_code=303)


@app.post("/profiles/{profile_id}/check")
def check_profile_html(profile_id: int, session: Session = Depends(get_session)):
    try:
        result = check_profile(session, profile_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    query = urlencode({"info": result.message, "checked_profile_id": result.profile_id})
    return RedirectResponse(url=f"/profiles?{query}", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    error: str = "",
    preview_job_id: int | None = None,
    edit_job_id: int | None = None,
    clone_job_id: int | None = None,
    session: Session = Depends(get_session),
):
    profiles = _list_profiles(session)
    jobs = _list_jobs(session)
    recent_runs = _list_runs(session, limit=50)
    interrupted_runs = _list_interrupted_runs(session, limit=5)
    schedule_examples = ["manual", "hourly", "daily", "weekly", "cron:0 6 * * *"]
    prefill = prefill_job_payload(request.query_params)
    requested_mode = "edit" if edit_job_id else "clone" if clone_job_id else "create"
    requested_job_id = edit_job_id or clone_job_id
    if requested_job_id and not prefill["name"] and not prefill["source_profile_id"] and not prefill["target_profile_id"]:
        existing_job = session.get(SyncJob, requested_job_id)
        if existing_job:
            prefill = build_job_prefill(existing_job, mode=requested_mode)
    active_runs = job_runner.active_run_snapshots()
    preview = None
    preview_transfer_warning = ""
    preview_checklist: list[str] = []
    if preview_job_id is not None:
        try:
            preview = preview_job(session, preview_job_id)
        except (LookupError, RcloneError) as exc:
            error = str(exc)
    indexed_profiles = {profile.id: profile for profile in profiles}
    indexed_jobs = {job.id: job for job in jobs}
    source_profile = indexed_profiles.get(prefill["source_profile_id"]) if prefill["source_profile_id"] else None
    target_profile = indexed_profiles.get(prefill["target_profile_id"]) if prefill["target_profile_id"] else None
    transfer_warning = job_transfer_warning(source_profile, target_profile)
    pre_run_checklist = job_pre_run_checklist(source_profile, target_profile)
    form_context = job_form_context(prefill)
    route_preview = route_preview_payload(
        source_profile,
        target_profile,
        source_path=prefill["source_path"],
        target_path=prefill["target_path"],
    )
    incidents_by_job = latest_problem_runs_by_job(recent_runs)
    if preview:
        preview_job_row = indexed_jobs.get(preview.job_id)
        if preview_job_row:
            preview_transfer_warning = job_transfer_warning(
                preview_job_row.source_profile,
                preview_job_row.target_profile,
            )
            preview_checklist = job_pre_run_checklist(
                preview_job_row.source_profile,
                preview_job_row.target_profile,
            )
    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "request": request,
            "jobs": jobs,
            "profiles": profiles,
            "schedule_examples": schedule_examples,
            "describe_schedule": describe_schedule,
            "decode_json": decode_json,
            "profile_type_label": profile_type_label,
            "status_label": status_label,
            "active_runs": active_runs,
            "has_active_runs": bool(active_runs),
            "format_bytes": format_bytes,
            "format_speed": format_speed,
            "format_eta": format_eta,
            "error": error,
            "preview": preview,
            "preview_transfer_warning": preview_transfer_warning,
            "preview_checklist": preview_checklist,
            "interrupted_runs": interrupted_runs,
            "prefill": prefill,
            "form_context": form_context,
            "route_preview": route_preview,
            "transfer_warning": transfer_warning,
            "pre_run_checklist": pre_run_checklist,
            "job_transfer_warning": job_transfer_warning,
            "job_pre_run_checklist": job_pre_run_checklist,
            "interrupted_run_recovery_steps": interrupted_run_recovery_steps,
            "job_requires_manual_start_checklist": job_requires_manual_start_checklist,
            "job_templates": suggested_job_templates(profiles),
            "incidents_by_job": incidents_by_job,
        },
    )


@app.post("/jobs")
def create_job_html(
    name: str = Form(...),
    source_profile_id: int = Form(...),
    source_path: str = Form(default=""),
    target_profile_id: int = Form(...),
    target_path: str = Form(default=""),
    schedule: str = Form(default="manual"),
    enabled: bool = Form(default=False),
    bandwidth_limit: str = Form(default=""),
    verify_checksum: bool = Form(default=False),
    filters: str = Form(default=""),
    session: Session = Depends(get_session),
):
    try:
        payload = SyncJobPayload(
            name=name,
            source_profile_id=source_profile_id,
            source_path=source_path,
            target_profile_id=target_profile_id,
            target_path=target_path,
            schedule=schedule,
            enabled=enabled,
            bandwidth_limit=bandwidth_limit or None,
            verify_checksum=verify_checksum,
            filters=filters,
        )
        source_profile, target_profile = resolve_job_profiles(
            session,
            source_profile_id=source_profile_id,
            target_profile_id=target_profile_id,
        )
        validate_job_route(
            source_profile,
            target_profile,
            source_path=payload.source_path,
            target_path=payload.target_path,
        )
        job = create_job(session, payload)
    except ValidationError as exc:
        first_error = exc.errors()[0]
        message = first_error.get("msg", "Проверьте заполнение формы задачи.")
        field = str((first_error.get("loc") or [""])[0])
        query = build_job_form_query(
            error=message,
            error_field=field,
            name=name,
            source_profile_id=source_profile_id,
            source_path=source_path,
            target_profile_id=target_profile_id,
            target_path=target_path,
            schedule=schedule,
            bandwidth_limit=bandwidth_limit,
            verify_checksum=verify_checksum,
            filters=filters,
            enabled=enabled,
        )
        return RedirectResponse(url=f"/jobs?{query}", status_code=303)
    except (LookupError, RcloneError) as exc:
        query = build_job_form_query(
            error=str(exc),
            error_field="source_path",
            name=name,
            source_profile_id=source_profile_id,
            source_path=source_path,
            target_profile_id=target_profile_id,
            target_path=target_path,
            schedule=schedule,
            bandwidth_limit=bandwidth_limit,
            verify_checksum=verify_checksum,
            filters=filters,
            enabled=enabled,
        )
        return RedirectResponse(url=f"/jobs?{query}", status_code=303)
    except JobConflictError as exc:
        query = build_job_form_query(
            error=str(exc),
            error_field="name",
            name=name,
            source_profile_id=source_profile_id,
            source_path=source_path,
            target_profile_id=target_profile_id,
            target_path=target_path,
            schedule=schedule,
            bandwidth_limit=bandwidth_limit,
            verify_checksum=verify_checksum,
            filters=filters,
            enabled=enabled,
        )
        return RedirectResponse(url=f"/jobs?{query}", status_code=303)
    job_runner.sync_job(job.id)
    return RedirectResponse(url="/jobs", status_code=303)


@app.post("/jobs/{job_id}/update")
def update_job_html(
    job_id: int,
    name: str = Form(...),
    source_profile_id: int = Form(...),
    source_path: str = Form(default=""),
    target_profile_id: int = Form(...),
    target_path: str = Form(default=""),
    schedule: str = Form(default="manual"),
    enabled: bool = Form(default=False),
    bandwidth_limit: str = Form(default=""),
    verify_checksum: bool = Form(default=False),
    filters: str = Form(default=""),
    session: Session = Depends(get_session),
):
    try:
        payload = SyncJobPayload(
            name=name,
            source_profile_id=source_profile_id,
            source_path=source_path,
            target_profile_id=target_profile_id,
            target_path=target_path,
            schedule=schedule,
            enabled=enabled,
            bandwidth_limit=bandwidth_limit or None,
            verify_checksum=verify_checksum,
            filters=filters,
        )
        source_profile, target_profile = resolve_job_profiles(
            session,
            source_profile_id=source_profile_id,
            target_profile_id=target_profile_id,
        )
        validate_job_route(
            source_profile,
            target_profile,
            source_path=payload.source_path,
            target_path=payload.target_path,
        )
        job = update_job(session, job_id, payload)
    except ValidationError as exc:
        first_error = exc.errors()[0]
        message = first_error.get("msg", "Проверьте заполнение формы задачи.")
        field = str((first_error.get("loc") or [""])[0])
        query = build_job_form_query(
            error=message,
            error_field=field,
            name=name,
            source_profile_id=source_profile_id,
            source_path=source_path,
            target_profile_id=target_profile_id,
            target_path=target_path,
            schedule=schedule,
            bandwidth_limit=bandwidth_limit,
            verify_checksum=verify_checksum,
            filters=filters,
            enabled=enabled,
            form_mode="edit",
            edit_job_id=job_id,
        )
        return RedirectResponse(url=f"/jobs?{query}", status_code=303)
    except (LookupError, RcloneError) as exc:
        query = build_job_form_query(
            error=str(exc),
            error_field="source_path",
            name=name,
            source_profile_id=source_profile_id,
            source_path=source_path,
            target_profile_id=target_profile_id,
            target_path=target_path,
            schedule=schedule,
            bandwidth_limit=bandwidth_limit,
            verify_checksum=verify_checksum,
            filters=filters,
            enabled=enabled,
            form_mode="edit",
            edit_job_id=job_id,
        )
        return RedirectResponse(url=f"/jobs?{query}", status_code=303)
    except JobConflictError as exc:
        query = build_job_form_query(
            error=str(exc),
            error_field="name",
            name=name,
            source_profile_id=source_profile_id,
            source_path=source_path,
            target_profile_id=target_profile_id,
            target_path=target_path,
            schedule=schedule,
            bandwidth_limit=bandwidth_limit,
            verify_checksum=verify_checksum,
            filters=filters,
            enabled=enabled,
            form_mode="edit",
            edit_job_id=job_id,
        )
        return RedirectResponse(url=f"/jobs?{query}", status_code=303)

    job_runner.sync_job(job.id)
    return RedirectResponse(url="/jobs", status_code=303)


@app.get("/jobs/{job_id}/start", response_class=HTMLResponse)
def manual_start_page(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = session.get(
        SyncJob,
        job_id,
        options=(selectinload(SyncJob.source_profile), selectinload(SyncJob.target_profile)),
    )
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    return templates.TemplateResponse(
        request=request,
        name="manual_start.html",
        context={
            "request": request,
            "job": job,
            "transfer_warning": job_transfer_warning(job.source_profile, job.target_profile),
            "pre_run_checklist": job_pre_run_checklist(job.source_profile, job.target_profile),
            "requires_checklist": job_requires_manual_start_checklist(job),
            "describe_schedule": describe_schedule,
        },
    )


@app.post("/jobs/{job_id}/run")
def run_job_html(job_id: int):
    ok, message = job_runner.enqueue_job(job_id, initiated_by="manual")
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return RedirectResponse(url="/runs", status_code=303)


@app.post("/runs/{run_id}/retry")
def retry_run_html(run_id: int, session: Session = Depends(get_session)):
    try:
        retry_run(session, run_id, job_runner)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunRecoveryError as exc:
        query = urlencode({"error": str(exc)})
        return RedirectResponse(url=f"/runs?{query}", status_code=303)
    return RedirectResponse(url="/runs", status_code=303)


@app.post("/jobs/{job_id}/preview")
def preview_job_html(job_id: int):
    query = urlencode({"preview_job_id": job_id})
    return RedirectResponse(url=f"/jobs?{query}", status_code=303)


@app.post("/jobs/{job_id}/pause")
def pause_job_html(job_id: int, session: Session = Depends(get_session)):
    job = session.get(SyncJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    job.enabled = False
    session.commit()
    job_runner.cancel_job(job_id)
    job_runner.sync_job(job_id)
    return RedirectResponse(url="/jobs", status_code=303)


@app.post("/jobs/{job_id}/resume")
def resume_job_html(job_id: int, session: Session = Depends(get_session)):
    job = session.get(SyncJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    job.enabled = True
    session.commit()
    job_runner.sync_job(job_id)
    return RedirectResponse(url="/jobs", status_code=303)


@app.post("/jobs/{job_id}/delete")
def delete_job_html(job_id: int, session: Session = Depends(get_session)):
    try:
        delete_job(session, job_id, job_runner)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobDeleteError as exc:
        query = urlencode({"error": str(exc)})
        return RedirectResponse(url=f"/jobs?{query}", status_code=303)
    return RedirectResponse(url="/jobs", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, error: str = "", session: Session = Depends(get_session)):
    runs = _list_runs(session)
    active_runs = job_runner.active_run_snapshots()
    interrupted_runs = _list_interrupted_runs(session, limit=5)
    incidents_by_job = latest_problem_runs_by_job(runs)
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={
            "request": request,
            "runs": runs,
            "active_runs": active_runs,
            "has_active_runs": bool(active_runs),
            "interrupted_runs": interrupted_runs,
            "status_label": status_label,
            "format_bytes": format_bytes,
            "format_speed": format_speed,
            "format_eta": format_eta,
            "job_transfer_warning": job_transfer_warning,
            "interrupted_run_recovery_steps": interrupted_run_recovery_steps,
            "job_requires_manual_start_checklist": job_requires_manual_start_checklist,
            "error": error,
            "incidents_by_job": incidents_by_job,
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(run_id: int, request: Request, session: Session = Depends(get_session)):
    run = session.get(RunHistory, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    active_runs = job_runner.active_run_snapshots()
    active_run = active_runs.get(run.job_id) if run.job_id else None
    route_preview = route_preview_payload(
        run.job.source_profile if run.job else None,
        run.job.target_profile if run.job else None,
        source_path=run.job.source_path if run.job else "",
        target_path=run.job.target_path if run.job else "",
    )
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={
            "request": request,
            "run": run,
            "active_run": active_run,
            "status_label": status_label,
            "format_bytes": format_bytes,
            "format_speed": format_speed,
            "format_eta": format_eta,
            "interrupted_run_recovery_steps": interrupted_run_recovery_steps,
            "job_requires_manual_start_checklist": job_requires_manual_start_checklist,
            "transfer_warning": job_transfer_warning(
                run.job.source_profile if run.job else None,
                run.job.target_profile if run.job else None,
            ),
            "route_preview": route_preview,
        },
    )


@app.post("/api/storage-profiles/test")
def api_test_profile(payload: ConnectionTestPayload):
    ok, message, command_preview = test_connection(payload)
    return {"ok": ok, "message": message, "command_preview": command_preview}


@app.get("/api/setup")
def api_setup():
    diagnostics = collect_setup_diagnostics()
    return {
        "rclone_available": diagnostics.rclone_available,
        "remotes": diagnostics.remotes,
        "local_candidates": diagnostics.local_candidates,
        "issues": diagnostics.issues,
    }


@app.post("/api/storage-profiles", response_model=StorageProfileRead)
def api_create_profile(payload: StorageProfilePayload, session: Session = Depends(get_session)):
    try:
        return create_profile(session, payload)
    except ProfileConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/storage-profiles/{profile_id}", response_model=StorageProfileRead)
def api_update_profile(
    profile_id: int,
    payload: StorageProfilePayload,
    clear_secret: bool = False,
    session: Session = Depends(get_session),
):
    try:
        return update_profile(session, profile_id, payload, clear_secret=clear_secret)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProfileConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/storage-profiles/{profile_id}")
def api_delete_profile(profile_id: int, session: Session = Depends(get_session)):
    try:
        delete_profile(session, profile_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProfileInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/storage-profiles", response_model=list[StorageProfileRead])
def api_list_profiles(session: Session = Depends(get_session)):
    return _list_profiles(session)


@app.get("/api/storage-profiles/{profile_id}/browse")
def api_browse_profile(profile_id: int, relative_path: str = "", session: Session = Depends(get_session)):
    profile = session.get(StorageProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден.")
    try:
        result: ProfileBrowseResult = browse_profile(profile, relative_path=relative_path)
    except RcloneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "profile_id": result.profile_id,
        "root_path": result.root_path,
        "relative_path": result.relative_path,
        "effective_path": result.effective_path,
        "entries": result.entries,
        "command_preview": result.command_preview,
    }


@app.post("/api/storage-profiles/{profile_id}/check")
def api_check_profile(profile_id: int, session: Session = Depends(get_session)):
    try:
        result: ProfileCheckResult = check_profile(session, profile_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "profile_id": result.profile_id,
        "ok": result.ok,
        "message": result.message,
        "command_preview": result.command_preview,
    }


@app.post("/api/jobs", response_model=SyncJobRead)
def api_create_job(payload: SyncJobPayload, session: Session = Depends(get_session)):
    try:
        source_profile, target_profile = resolve_job_profiles(
            session,
            source_profile_id=payload.source_profile_id,
            target_profile_id=payload.target_profile_id,
        )
        validate_job_route(
            source_profile,
            target_profile,
            source_path=payload.source_path,
            target_path=payload.target_path,
        )
        job = create_job(session, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RcloneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    job_runner.sync_job(job.id)
    return job


@app.put("/api/jobs/{job_id}", response_model=SyncJobRead)
def api_update_job(job_id: int, payload: SyncJobPayload, session: Session = Depends(get_session)):
    try:
        source_profile, target_profile = resolve_job_profiles(
            session,
            source_profile_id=payload.source_profile_id,
            target_profile_id=payload.target_profile_id,
        )
        validate_job_route(
            source_profile,
            target_profile,
            source_path=payload.source_path,
            target_path=payload.target_path,
        )
        job = update_job(session, job_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RcloneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    job_runner.sync_job(job.id)
    return job


@app.get("/api/jobs", response_model=list[SyncJobRead])
def api_list_jobs(session: Session = Depends(get_session)):
    return _list_jobs(session)


@app.get("/api/runtime/jobs")
def api_runtime_jobs():
    return {"active_runs": job_runner.active_run_snapshots()}


@app.post("/api/jobs/{job_id}/run")
def api_run_job(job_id: int):
    ok, message = job_runner.enqueue_job(job_id, initiated_by="manual")
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message}


@app.post("/api/runs/{run_id}/retry")
def api_retry_run(run_id: int, session: Session = Depends(get_session)):
    try:
        job_id, message = retry_run(session, run_id, job_runner)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunRecoveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job_id": job_id, "message": message}


@app.post("/api/jobs/{job_id}/preview")
def api_preview_job(job_id: int, session: Session = Depends(get_session)):
    try:
        preview = preview_job(session, job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RcloneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "job_id": preview.job_id,
        "job_name": preview.job_name,
        "source": preview.source,
        "target": preview.target,
        "command_preview": preview.command_preview,
        "size_command_preview": preview.size_command_preview,
        "source_bytes": preview.source_bytes,
        "source_files": preview.source_files,
        "dry_run_ok": preview.dry_run_result.ok,
        "dry_run_stdout": preview.dry_run_result.stdout,
        "dry_run_stderr": preview.dry_run_result.stderr,
        "dry_run_returncode": preview.dry_run_result.returncode,
        "dry_run_bytes": preview.dry_run_result.bytes_transferred,
        "dry_run_files": preview.dry_run_result.files_transferred,
    }


@app.post("/api/jobs/{job_id}/pause")
def api_pause_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(SyncJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    job.enabled = False
    session.commit()
    canceled = job_runner.cancel_job(job_id)
    job_runner.sync_job(job_id)
    return {"ok": True, "canceled_running_job": canceled}


@app.post("/api/jobs/{job_id}/resume")
def api_resume_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(SyncJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    job.enabled = True
    session.commit()
    job_runner.sync_job(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: int, session: Session = Depends(get_session)):
    try:
        delete_job(session, job_id, job_runner)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobDeleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/runs", response_model=list[RunHistoryRead])
def api_runs(session: Session = Depends(get_session)):
    return _list_runs(session)

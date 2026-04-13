from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
from app.services.jobs import create_job, init_settings, job_runner
from app.services.profiles import (
    ProfileConflictError,
    ProfileInUseError,
    build_s3_remote_root,
    create_profile,
    delete_profile,
    split_s3_remote_root,
    test_connection,
    update_profile,
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


def profile_connection_hint(profile: StorageProfile, configured_remotes: set[str]) -> str:
    if profile.profile_type not in {ProfileType.S3_REMOTE.value, ProfileType.YANDEX_REMOTE.value}:
        return ""

    remote_name, _, _ = split_s3_remote_root(profile.root_path_or_remote)
    if not remote_name:
        return "У профиля не указан `rclone remote`."
    if remote_name in configured_remotes:
        return ""

    known = ", ".join(f"{item}:" for item in sorted(configured_remotes)) or "нет настроенных remote"
    return f"Remote `{remote_name}:` не найден в `rclone`. Сейчас доступны: {known}."


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
        "name": data.get("suggested_name", ""),
        "source_profile_id": int(data["source_profile_id"]) if data.get("source_profile_id") else None,
        "source_path": data.get("source_path", ""),
        "target_profile_id": int(data["target_profile_id"]) if data.get("target_profile_id") else None,
        "target_path": data.get("target_path", ""),
        "schedule": data.get("schedule", "manual"),
        "bandwidth_limit": data.get("bandwidth_limit", ""),
        "filters": data.get("filters", ""),
        "enabled": str(data.get("enabled", "true")).lower() not in {"false", "0", ""},
        "verify_checksum": str(data.get("verify_checksum", "false")).lower() in {"true", "1", "on"},
    }


def build_profile_prefill(
    *,
    suggested_name: str = "",
    suggested_type: str = "",
    suggested_root: str = "",
    suggested_options_json: str = "{}",
    edit_profile_id: int | None = None,
) -> dict[str, Any]:
    options = decode_json(suggested_options_json, {})
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
        .options(selectinload(RunHistory.job))
        .order_by(desc(RunHistory.started_at))
        .limit(limit)
    ).all()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    stats = job_runner.status()
    recent_runs = _list_runs(session, limit=10)
    diagnostics = collect_setup_diagnostics()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "stats": stats,
            "recent_runs": recent_runs,
            "diagnostics": diagnostics,
            "status_label": status_label,
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


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page(
    request: Request,
    suggested_name: str = "",
    suggested_type: str = "",
    suggested_root: str = "",
    suggested_options_json: str = "{}",
    edit_profile_id: int | None = None,
    error: str = "",
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
            "configured_remotes": configured_remotes,
            "prefill": build_profile_prefill(
                suggested_name=suggested_name,
                suggested_type=suggested_type,
                suggested_root=suggested_root,
                suggested_options_json=suggested_options_json,
                edit_profile_id=edit_profile_id,
            ),
            "error": error,
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


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, session: Session = Depends(get_session)):
    profiles = _list_profiles(session)
    jobs = _list_jobs(session)
    schedule_examples = ["manual", "hourly", "daily", "weekly", "cron:0 6 * * *"]
    prefill = prefill_job_payload(request.query_params)
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
            "prefill": prefill,
            "job_templates": suggested_job_templates(profiles),
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
    job = create_job(session, payload)
    job_runner.sync_job(job.id)
    return RedirectResponse(url="/jobs", status_code=303)


@app.post("/jobs/{job_id}/run")
def run_job_html(job_id: int):
    ok, message = job_runner.enqueue_job(job_id, initiated_by="manual")
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return RedirectResponse(url="/runs", status_code=303)


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


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, session: Session = Depends(get_session)):
    runs = _list_runs(session)
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={"request": request, "runs": runs, "status_label": status_label},
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(run_id: int, request: Request, session: Session = Depends(get_session)):
    run = session.get(RunHistory, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден.")
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={"request": request, "run": run, "status_label": status_label},
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


@app.post("/api/jobs", response_model=SyncJobRead)
def api_create_job(payload: SyncJobPayload, session: Session = Depends(get_session)):
    job = create_job(session, payload)
    job_runner.sync_job(job.id)
    return job


@app.get("/api/jobs", response_model=list[SyncJobRead])
def api_list_jobs(session: Session = Depends(get_session)):
    return _list_jobs(session)


@app.post("/api/jobs/{job_id}/run")
def api_run_job(job_id: int):
    ok, message = job_runner.enqueue_job(job_id, initiated_by="manual")
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message}


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


@app.get("/api/runs", response_model=list[RunHistoryRead])
def api_runs(session: Session = Depends(get_session)):
    return _list_runs(session)

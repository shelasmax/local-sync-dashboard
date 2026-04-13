from __future__ import annotations

from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import ProfileType, StorageProfile, SyncJob
from app.schemas import ConnectionTestPayload, StorageProfilePayload
from app.services.common import decode_json, encode_json, join_local_path, join_remote_path
from app.services.keychain import delete_secret, store_secret
from app.services.rclone import RcloneError, test_remote


class ProfileConflictError(ValueError):
    pass


class ProfileInUseError(ValueError):
    pass


def split_s3_remote_root(root_path_or_remote: str) -> tuple[str, str, str]:
    remote_name, _, remainder = (root_path_or_remote or "").partition(":")
    remainder = remainder.lstrip("/")
    bucket, _, prefix = remainder.partition("/")
    return remote_name.strip(), bucket.strip(), prefix.strip("/")


def build_s3_remote_root(remote_name: str, bucket: str = "", prefix: str = "") -> str:
    normalized_remote = remote_name.strip().rstrip(":")
    normalized_bucket = bucket.strip().strip("/")
    normalized_prefix = prefix.strip().strip("/")

    path_parts = [part for part in [normalized_bucket, normalized_prefix] if part]
    suffix = "/".join(path_parts)
    return f"{normalized_remote}:{suffix}" if suffix else f"{normalized_remote}:"


def deserialize_profile_options(profile: StorageProfile) -> dict:
    return decode_json(profile.options_json, {})


def storage_endpoint(profile: StorageProfile, relative_path: str = "") -> str:
    if profile.profile_type in {ProfileType.LOCAL_FOLDER.value, ProfileType.SYNOLOGY_SHARE.value}:
        return join_local_path(profile.root_path_or_remote, relative_path)
    return join_remote_path(profile.root_path_or_remote, relative_path)


def test_connection(payload: ConnectionTestPayload | StorageProfilePayload) -> tuple[bool, str, str | None]:
    if payload.profile_type in {ProfileType.LOCAL_FOLDER, ProfileType.SYNOLOGY_SHARE}:
        candidate = Path(payload.root_path_or_remote).expanduser()
        if not candidate.exists():
            return False, f"Путь не существует: {candidate}", None
        if not candidate.is_dir():
            return False, f"Путь не является директорией: {candidate}", None
        if not candidate.stat():
            return False, f"Не удалось проверить путь: {candidate}", None
        return True, f"Локальный путь доступен: {candidate}", None

    endpoint = join_remote_path(payload.root_path_or_remote)
    try:
        result = test_remote(endpoint)
    except RcloneError as exc:
        return False, str(exc), None

    output = result.stdout.strip() or result.stderr.strip() or "Проверка remote завершена."
    return result.ok, output, result.command_preview


def create_profile(session: Session, payload: StorageProfilePayload) -> StorageProfile:
    existing = session.scalar(select(StorageProfile).where(StorageProfile.name == payload.name))
    if existing:
        raise ProfileConflictError(f"Профиль с именем «{payload.name}» уже существует.")

    options = dict(payload.options)
    root_path = payload.root_path_or_remote
    if payload.profile_type in {ProfileType.LOCAL_FOLDER, ProfileType.SYNOLOGY_SHARE}:
        root_path = str(Path(root_path).expanduser())

    secret_ref = None
    if payload.secret:
        secret_ref = store_secret(payload.name, payload.secret)

    ok, _, _ = test_connection(payload)
    profile = StorageProfile(
        name=payload.name,
        profile_type=payload.profile_type.value,
        root_path_or_remote=root_path,
        status="ready" if ok else "unreachable",
        secret_ref=secret_ref,
        options_json=encode_json(options),
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile


def update_profile(
    session: Session,
    profile_id: int,
    payload: StorageProfilePayload,
    *,
    clear_secret: bool = False,
) -> StorageProfile:
    profile = session.get(StorageProfile, profile_id)
    if not profile:
        raise LookupError("Профиль не найден.")

    existing = session.scalar(
        select(StorageProfile).where(
            StorageProfile.name == payload.name,
            StorageProfile.id != profile_id,
        )
    )
    if existing:
        raise ProfileConflictError(f"Профиль с именем «{payload.name}» уже существует.")

    options = dict(payload.options)
    root_path = payload.root_path_or_remote
    if payload.profile_type in {ProfileType.LOCAL_FOLDER, ProfileType.SYNOLOGY_SHARE}:
        root_path = str(Path(root_path).expanduser())

    if clear_secret and profile.secret_ref:
        delete_secret(profile.secret_ref)
        profile.secret_ref = None

    if payload.secret:
        new_secret_ref = store_secret(payload.name, payload.secret)
        if profile.secret_ref and profile.secret_ref != new_secret_ref:
            delete_secret(profile.secret_ref)
        profile.secret_ref = new_secret_ref

    ok, _, _ = test_connection(payload)
    profile.name = payload.name
    profile.profile_type = payload.profile_type.value
    profile.root_path_or_remote = root_path
    profile.status = "ready" if ok else "unreachable"
    profile.options_json = encode_json(options)
    session.commit()
    session.refresh(profile)
    return profile


def delete_profile(session: Session, profile_id: int) -> None:
    profile = session.get(StorageProfile, profile_id)
    if not profile:
        raise LookupError("Профиль не найден.")

    linked_job = session.scalar(
        select(SyncJob.id).where(
            or_(
                SyncJob.source_profile_id == profile_id,
                SyncJob.target_profile_id == profile_id,
            )
        )
    )
    if linked_job:
        raise ProfileInUseError("Профиль используется в задаче синхронизации. Сначала измените или удалите эту задачу.")

    if profile.secret_ref:
        delete_secret(profile.secret_ref)
    session.delete(profile)
    session.commit()

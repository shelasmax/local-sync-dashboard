from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.services.rclone import is_available

IGNORED_VOLUME_PREFIXES = ("com.apple.", ".")
IGNORED_VOLUME_FRAGMENTS = ("TimeMachine", "Резервные копии", "Backup")


@dataclass(slots=True)
class SetupDiagnostics:
    rclone_available: bool
    remotes: list[str]
    local_candidates: dict[str, list[str]]
    issues: list[str]


def list_rclone_remotes() -> tuple[list[str], str | None]:
    if not is_available():
        return [], "rclone не установлен."

    result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or "Не удалось прочитать список remote-подключений rclone."
        if "Config file" in message and "not found" in message:
            return [], None
        return [], message

    remotes = [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]
    return remotes, None


def discover_local_candidates(home: Path | None = None, volumes_dir: Path | None = None) -> dict[str, list[str]]:
    user_home = home or Path.home()
    volumes_root = volumes_dir or Path("/Volumes")

    icloud_candidates = _existing(
        [
            user_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
        ]
    )
    google_candidates = _existing(sorted((user_home / "Library" / "CloudStorage").glob("GoogleDrive*")))
    synology_candidates = _existing(
        [
            path
            for path in volumes_root.iterdir()
            if path.is_dir()
            and path.name not in {"Macintosh HD"}
            and not path.name.startswith(IGNORED_VOLUME_PREFIXES)
            and not any(fragment in path.name for fragment in IGNORED_VOLUME_FRAGMENTS)
        ]
        if volumes_root.exists()
        else []
    )

    return {
        "icloud": icloud_candidates,
        "google_drive": google_candidates,
        "mounted_volumes": synology_candidates,
    }


_DIAGNOSTICS_CACHE: tuple[float, SetupDiagnostics] | None = None
_DIAGNOSTICS_TTL = 30.0


def collect_setup_diagnostics() -> SetupDiagnostics:
    global _DIAGNOSTICS_CACHE
    now = time.monotonic()
    if _DIAGNOSTICS_CACHE and (now - _DIAGNOSTICS_CACHE[0]) < _DIAGNOSTICS_TTL:
        return _DIAGNOSTICS_CACHE[1]

    remotes, remote_error = list_rclone_remotes()
    local_candidates = discover_local_candidates()
    issues: list[str] = []

    if not is_available():
        issues.append("Установите rclone перед созданием профилей для S3 или Яндекс Диска.")
    if remote_error:
        issues.append(remote_error)
    if not remotes:
        issues.append("Remote-подключения rclone пока не настроены. Для S3 и Яндекс Диска сначала нужен `rclone config`.")
    if not local_candidates["mounted_volumes"]:
        issues.append("В /Volumes не найдены смонтированные внешние диски или NAS-шары.")

    result = SetupDiagnostics(
        rclone_available=is_available(),
        remotes=remotes,
        local_candidates=local_candidates,
        issues=issues,
    )
    _DIAGNOSTICS_CACHE = (now, result)
    return result


def _existing(paths: list[Path]) -> list[str]:
    return [str(path) for path in paths if path.exists()]

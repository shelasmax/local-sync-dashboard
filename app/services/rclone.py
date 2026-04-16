from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import selectors
from dataclasses import dataclass
from math import isfinite
from threading import Event
from time import monotonic
from typing import Callable

from app.services.common import parse_plain_stats


class RcloneError(RuntimeError):
    pass


@dataclass(slots=True)
class RcloneResult:
    returncode: int
    stdout: str
    stderr: str
    command_preview: str
    bytes_transferred: int = 0
    files_transferred: int = 0
    canceled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.canceled


@dataclass(slots=True)
class RcloneSizeResult:
    bytes_total: int = 0
    files_total: int = 0
    command_preview: str = ""


@dataclass(slots=True)
class RcloneProgress:
    bytes_transferred: int = 0
    total_bytes: int = 0
    files_transferred: int = 0
    total_files: int = 0
    speed: float = 0
    eta_seconds: int | None = None
    listed: int = 0
    checks: int = 0
    total_checks: int = 0
    active_items: list[dict[str, object]] | None = None


def is_available() -> bool:
    return shutil.which("rclone") is not None


def ensure_available() -> None:
    if not is_available():
        raise RcloneError("rclone is not installed or not available in PATH.")


def build_copy_command(
    source: str,
    target: str,
    filters: list[str] | None = None,
    bandwidth_limit: str | None = None,
    verify_checksum: bool = False,
    transfers: int | None = None,
    checkers: int | None = None,
    fast_list: bool = False,
    dry_run: bool = False,
) -> list[str]:
    command = [
        "rclone",
        "copy",
        source,
        target,
        "--create-empty-src-dirs",
        "--stats=1s",
        "--stats-log-level",
        "NOTICE",
        "--use-json-log",
    ]
    if verify_checksum:
        command.append("--checksum")
    if dry_run:
        command.append("--dry-run")
    if bandwidth_limit:
        command.extend(["--bwlimit", bandwidth_limit])
    if transfers is not None:
        command.extend(["--transfers", str(transfers)])
    if checkers is not None:
        command.extend(["--checkers", str(checkers)])
    if fast_list:
        command.append("--fast-list")
    for filter_rule in filters or []:
        command.extend(["--filter", filter_rule])
    return command


def test_remote(endpoint: str, timeout_seconds: int = 30) -> RcloneResult:
    ensure_available()
    primary = ["rclone", "about", endpoint, "--json"]
    result = subprocess.run(primary, capture_output=True, text=True, check=False, timeout=timeout_seconds)
    preview = shlex.join(primary)
    if result.returncode == 0:
        return RcloneResult(result.returncode, result.stdout, result.stderr, preview)

    fallback = ["rclone", "lsf", endpoint, "--max-depth", "1"]
    fallback_result = subprocess.run(
        fallback,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    return RcloneResult(
        fallback_result.returncode,
        fallback_result.stdout,
        "\n".join(part for part in [result.stderr, fallback_result.stderr] if part),
        shlex.join(fallback),
    )


def estimate_size(endpoint: str, filters: list[str] | None = None, timeout_seconds: int = 60) -> RcloneSizeResult:
    ensure_available()
    command = ["rclone", "size", endpoint, "--json"]
    for filter_rule in filters or []:
        command.extend(["--filter", filter_rule])
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout_seconds)
    if result.returncode != 0:
        raise RcloneError(summarize_rclone_error(result.stderr.strip(), returncode=result.returncode))
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RcloneError("Не удалось разобрать ответ rclone size.") from exc
    return RcloneSizeResult(
        bytes_total=int(payload.get("bytes", 0) or 0),
        files_total=int(payload.get("count", 0) or 0),
        command_preview=shlex.join(command),
    )


def list_directories(endpoint: str, timeout_seconds: int = 30) -> tuple[list[str], str]:
    ensure_available()
    command = ["rclone", "lsf", endpoint, "--dirs-only", "--max-depth", "1"]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout_seconds)
    if result.returncode != 0:
        raise RcloneError(summarize_rclone_error(result.stderr.strip(), returncode=result.returncode))
    entries = [
        line.strip().rstrip("/")
        for line in (result.stdout or "").splitlines()
        if line.strip()
    ]
    return entries, shlex.join(command)


def execute(command: list[str], timeout_seconds: int, cancel_event: Event | None = None) -> RcloneResult:
    ensure_available()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    return _stream_process(process, command, timeout_seconds, cancel_event=cancel_event)


def execute_with_progress(
    command: list[str],
    timeout_seconds: int,
    progress_callback: Callable[[RcloneProgress], None] | None = None,
    cancel_event: Event | None = None,
) -> RcloneResult:
    ensure_available()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    return _stream_process(
        process,
        command,
        timeout_seconds,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def summarize_rclone_error(output: str, *, returncode: int | None = None) -> str:
    messages = _extract_rclone_messages(output)
    condensed = " ".join(messages[:2]).strip() if messages else output.strip()
    lowered = condensed.lower()
    raw_lowered = output.lower()

    if "timed out after" in raw_lowered:
        match = re.search(r"timed out after\s+(\d+)\s+seconds", output, flags=re.IGNORECASE)
        if match:
            return f"Превышен лимит времени запуска: {match.group(1)} сек."
        return "Превышен лимит времени запуска."

    if "directory not found" in lowered:
        if "local file system at /home/" in raw_lowered:
            return (
                "Профиль Synology, похоже, указывает не на macOS mount path из `/Volumes`. "
                "Исправьте корень профиля и не используйте путь вида `/home/...`."
            )
        if "/volumes/" in raw_lowered and "source root directory" in lowered:
            match = re.search(r"local file system at ([^\" ]+)", output, flags=re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                base = candidate.rstrip("/").split("/")[-1]
                parent = candidate.rstrip("/")
                if base and parent.lower().endswith(f"/{base.lower()}/{base.lower()}"):
                    return (
                        "Source path дублирует последнюю папку профиля. "
                        "Оставьте `source_path` пустым или поднимите корень профиля на уровень выше."
                    )
        if any(fragment in lowered for fragment in ("source root directory", "error listing", "failed to size", "failed to copy")):
            return (
                "Источник или подпапка не найдены в выбранном хранилище. "
                "Проверьте профиль-источник и поле «Подпапка в источнике»."
            )
        return "Одна из директорий не найдена. Проверьте source path, target path и доступность нужного remote или mount path."

    if "stale file handle" in lowered or "transport endpoint is not connected" in lowered:
        return (
            "Mount path стал недоступен после sleep, reconnect или переподключения сети. "
            "Переподключите том в `/Volumes`, проверьте профиль и повторите запуск."
        )

    if "didn't find section in config file" in lowered or "didn't find section" in lowered or "config file" in lowered:
        return "Нужный rclone remote не найден в конфигурации этого Mac. Проверьте setup и имя remote в профиле."

    if "unauthorized" in lowered or "access denied" in lowered or "forbidden" in lowered or "401" in lowered or "403" in lowered:
        return "Удаленное хранилище отклонило доступ. Проверьте авторизацию remote и права на bucket или папку."

    if "timeout" in lowered or "temporarily unavailable" in lowered or "network is unreachable" in lowered or "connection refused" in lowered:
        return "Похоже на сетевую ошибку или недоступный remote. Проверьте интернет, mount path и повторите запуск."

    if condensed:
        return condensed
    if returncode is not None:
        return f"rclone завершился с кодом {returncode}."
    return "rclone завершился с ошибкой."


def is_timeout_output(output: str) -> bool:
    return "timed out after" in output.lower()


def _extract_rclone_messages(output: str) -> list[str]:
    messages: list[str] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                pass
            else:
                message = str(payload.get("msg", "")).strip()
                if message:
                    messages.append(message)
                obj = str(payload.get("object", "")).strip()
                if obj:
                    messages.append(obj)
                if message or obj:
                    continue
        messages.append(line)

    if not messages:
        for match in re.findall(r'"msg"\s*:\s*"([^"]+)"', output):
            message = bytes(match, "utf-8").decode("unicode_escape").strip()
            if message:
                messages.append(message)

    deduped: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if message not in seen:
            deduped.append(message)
            seen.add(message)
    return deduped


_GRACE_SECONDS = 5


def _stream_process(
    process: subprocess.Popen[str],
    command: list[str],
    timeout_seconds: int,
    progress_callback: Callable[[RcloneProgress], None] | None = None,
    cancel_event: Event | None = None,
) -> RcloneResult:
    canceled = False
    timed_out = False
    killed = False
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    deadline = monotonic() + timeout_seconds
    selector = selectors.DefaultSelector()

    if process.stdout:
        selector.register(process.stdout, selectors.EVENT_READ, data="stdout")
    if process.stderr:
        selector.register(process.stderr, selectors.EVENT_READ, data="stderr")

    def _ensure_dead() -> None:
        """SIGTERM → grace period → SIGKILL for stubborn processes."""
        nonlocal killed
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            killed = True

    try:
        while True:
            if cancel_event and cancel_event.is_set() and process.poll() is None:
                canceled = True
                _ensure_dead()
            if monotonic() >= deadline and process.poll() is None:
                timed_out = True
                _ensure_dead()

            if not selector.get_map():
                if process.poll() is not None:
                    break
                continue

            try:
                events = selector.select(timeout=1)
            except OSError:
                break

            if not events and process.poll() is not None:
                for key in list(selector.get_map().values()):
                    stream = key.fileobj
                    try:
                        remainder = stream.read()
                    except (OSError, ValueError):
                        remainder = ""
                    if remainder:
                        if key.data == "stdout":
                            stdout_chunks.append(remainder)
                        else:
                            stderr_chunks.append(remainder)
                    try:
                        selector.unregister(stream)
                    except KeyError:
                        pass
                    stream.close()
                break

            for key, _ in events:
                stream = key.fileobj
                try:
                    line = stream.readline()
                except (OSError, ValueError):
                    try:
                        selector.unregister(stream)
                    except KeyError:
                        pass
                    stream.close()
                    continue
                if line == "":
                    try:
                        selector.unregister(stream)
                    except KeyError:
                        pass
                    stream.close()
                    continue
                if key.data == "stdout":
                    stdout_chunks.append(line)
                else:
                    stderr_chunks.append(line)
                if progress_callback:
                    progress = parse_progress_line(line)
                    if progress:
                        progress_callback(progress)
    finally:
        for key in list(selector.get_map().values()) if selector.get_map() else []:
            try:
                selector.unregister(key.fileobj)
            except KeyError:
                pass
            try:
                key.fileobj.close()
            except (OSError, ValueError):
                pass
        selector.close()
        try:
            process.wait(timeout=_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    if timed_out:
        stderr = "\n".join(part for part in [stderr, f"Timed out after {timeout_seconds} seconds."] if part)
    if killed:
        stderr = "\n".join(part for part in [stderr, "Process killed (SIGKILL) after refusing to terminate."] if part)

    combined = "\n".join(part for part in [stdout, stderr] if part)
    bytes_transferred, files_transferred = _extract_stats(combined)
    return RcloneResult(
        returncode=124 if timed_out else (process.returncode or 0),
        stdout=stdout,
        stderr=stderr,
        command_preview=shlex.join(command),
        bytes_transferred=bytes_transferred,
        files_transferred=files_transferred,
        canceled=canceled,
    )


def parse_progress_line(line: str) -> RcloneProgress | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None

    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None

    eta_value = stats.get("eta")
    eta_seconds: int | None
    if isinstance(eta_value, (int, float)) and isfinite(eta_value) and eta_value < 9_000_000_000:
        eta_seconds = max(0, int(eta_value))
    else:
        eta_seconds = None

    active_items: list[dict[str, object]] = []
    for item in stats.get("transferring", []) or []:
        if not isinstance(item, dict):
            continue
        active_items.append(
            {
                "name": str(item.get("name", "")),
                "bytes": int(item.get("bytes", 0) or 0),
                "size": int(item.get("size", 0) or 0),
                "percentage": int(item.get("percentage", 0) or 0),
                "speed": float(item.get("speed", 0) or 0),
                "eta_seconds": int(item["eta"]) if isinstance(item.get("eta"), (int, float)) and isfinite(item["eta"]) else None,
            }
        )

    return RcloneProgress(
        bytes_transferred=int(stats.get("bytes", 0) or 0),
        total_bytes=int(stats.get("totalBytes", 0) or 0),
        files_transferred=int(stats.get("transfers", 0) or 0),
        total_files=int(stats.get("totalTransfers", 0) or 0),
        speed=float(stats.get("speed", 0) or 0),
        eta_seconds=eta_seconds,
        listed=int(stats.get("listed", 0) or 0),
        checks=int(stats.get("checks", 0) or 0),
        total_checks=int(stats.get("totalChecks", 0) or 0),
        active_items=active_items,
    )


def _extract_stats(output: str) -> tuple[int, int]:
    last_stats: dict[str, int] | None = None
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        stats = payload.get("stats")
        if isinstance(stats, dict):
            last_stats = stats
    if last_stats:
        return int(last_stats.get("bytes", 0)), int(last_stats.get("transfers", 0))
    return parse_plain_stats(output)

from __future__ import annotations

import json
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
    if bandwidth_limit:
        command.extend(["--bwlimit", bandwidth_limit])
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


def _stream_process(
    process: subprocess.Popen[str],
    command: list[str],
    timeout_seconds: int,
    progress_callback: Callable[[RcloneProgress], None] | None = None,
    cancel_event: Event | None = None,
) -> RcloneResult:
    canceled = False
    timed_out = False
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    deadline = monotonic() + timeout_seconds
    selector = selectors.DefaultSelector()

    if process.stdout:
        selector.register(process.stdout, selectors.EVENT_READ, data="stdout")
    if process.stderr:
        selector.register(process.stderr, selectors.EVENT_READ, data="stderr")

    while True:
        if cancel_event and cancel_event.is_set() and process.poll() is None:
            canceled = True
            process.terminate()
        if monotonic() >= deadline and process.poll() is None:
            timed_out = True
            process.terminate()

        if not selector.get_map():
            if process.poll() is not None:
                break
            continue

        try:
            events = selector.select(timeout=1)
        except OSError as exc:
            raise RcloneError(str(exc)) from exc

        if not events and process.poll() is not None:
            for key in list(selector.get_map().values()):
                stream = key.fileobj
                remainder = stream.read()
                if remainder:
                    if key.data == "stdout":
                        stdout_chunks.append(remainder)
                    else:
                        stderr_chunks.append(remainder)
                selector.unregister(stream)
                stream.close()
            break

        for key, _ in events:
            stream = key.fileobj
            line = stream.readline()
            if line == "":
                selector.unregister(stream)
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

    selector.close()
    process.wait()

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    if timed_out:
        stderr = "\n".join(part for part in [stderr, f"Timed out after {timeout_seconds} seconds."] if part)

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

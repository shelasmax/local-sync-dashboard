from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from threading import Event
from time import monotonic

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
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    canceled = False
    timed_out = False
    stdout = ""
    stderr = ""
    deadline = monotonic() + timeout_seconds

    while True:
        if cancel_event and cancel_event.is_set() and process.poll() is None:
            canceled = True
            process.terminate()
        if monotonic() >= deadline and process.poll() is None:
            timed_out = True
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            continue
        except subprocess.SubprocessError as exc:
            raise RcloneError(str(exc)) from exc

    if canceled and process.returncode is None:
        process.kill()
        stdout, stderr = process.communicate()
    elif timed_out and process.returncode is None:
        process.kill()
        stdout, stderr = process.communicate()
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

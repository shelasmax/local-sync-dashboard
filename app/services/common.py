from __future__ import annotations

import json
import re
from pathlib import Path


TEMPORARY_NETWORK_PATTERNS = (
    "timeout",
    "temporarily unavailable",
    "temporary failure",
    "connection reset",
    "tls handshake timeout",
    "network is unreachable",
    "i/o timeout",
    "connection refused",
)


def join_local_path(root: str, child: str = "") -> str:
    base = Path(root).expanduser()
    relative = child.strip().lstrip("/")
    return str(base / relative) if relative else str(base)


def normalize_remote_root(value: str) -> str:
    remote = value.strip()
    if ":" not in remote:
        return f"{remote}:"
    return remote


def join_remote_path(root: str, child: str = "") -> str:
    remote_root = normalize_remote_root(root)
    relative = child.strip().lstrip("/")
    if not relative:
        return remote_root
    if remote_root.endswith(":"):
        return f"{remote_root}{relative}"
    return f"{remote_root.rstrip('/')}/{relative}"


def decode_json(text: str | None, fallback):
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


def encode_json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def is_temporary_network_error(output: str) -> bool:
    lowered = output.lower()
    return any(fragment in lowered for fragment in TEMPORARY_NETWORK_PATTERNS)


def parse_plain_stats(output: str) -> tuple[int, int]:
    bytes_transferred = 0
    files_transferred = 0

    bytes_match = re.findall(r'"bytes"\s*:\s*(\d+)', output)
    transfers_match = re.findall(r'"transfers"\s*:\s*(\d+)', output)
    if bytes_match:
        bytes_transferred = int(bytes_match[-1])
    if transfers_match:
        files_transferred = int(transfers_match[-1])

    return bytes_transferred, files_transferred


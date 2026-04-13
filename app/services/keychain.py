from __future__ import annotations

import subprocess

from app.config import settings


class KeychainError(RuntimeError):
    pass


def _service_name(account: str) -> str:
    return f"{settings.keychain_service_prefix}.{account}"


def store_secret(account: str, secret: str) -> str:
    command = [
        "security",
        "add-generic-password",
        "-U",
        "-a",
        account,
        "-s",
        _service_name(account),
        "-w",
        secret,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise KeychainError(result.stderr.strip() or "Failed to save secret to macOS Keychain.")
    return account


def load_secret(account: str) -> str | None:
    command = [
        "security",
        "find-generic-password",
        "-a",
        account,
        "-s",
        _service_name(account),
        "-w",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def delete_secret(account: str) -> None:
    command = [
        "security",
        "delete-generic-password",
        "-a",
        account,
        "-s",
        _service_name(account),
    ]
    subprocess.run(command, capture_output=True, text=True, check=False)


"""Credential storage, preferring the macOS Keychain over a file on disk.

A `.env` outside the repository fixes the accidental-leak paths — a stray
``git add -f``, a ``zip -r`` of the project, a shared folder, another account
on the machine. It does not encrypt anything: the keys are still plaintext,
still readable by anything running as you, and still copied verbatim into
every Time Machine snapshot and backup that touches the home directory.

The Keychain fixes those: encrypted at rest, sealed with the login keychain,
and backed up as ciphertext.

It is not a sandbox. Any process running as you can shell out to
``security find-generic-password`` and read the same value, so this raises
the cost of a leak rather than making one impossible. Treat it as protection
against spillage and backups, not against code you have already run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

SERVICE = "wheelscan"
KEYS = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")

_SECURITY = "/usr/bin/security"


class KeychainError(RuntimeError):
    pass


def available() -> bool:
    """True when the macOS `security` tool is present."""
    return sys.platform == "darwin" and shutil.which(_SECURITY) is not None


def get(name: str, service: str = SERVICE) -> str | None:
    """Read one secret. Returns None when it is not stored."""
    if not available():
        return None
    try:
        result = subprocess.run(
            [_SECURITY, "find-generic-password", "-a", name, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def put(name: str, value: str, service: str = SERVICE) -> None:
    """Store or replace one secret.

    The value passes through argv, so it is briefly visible to ``ps`` on this
    machine. `security` offers no stdin form that stores a non-empty password,
    and this runs once at setup rather than on every scan.
    """
    if not available():
        raise KeychainError("The macOS Keychain is only available on macOS.")
    if not value:
        raise KeychainError(f"Refusing to store an empty value for {name}.")
    result = subprocess.run(
        [_SECURITY, "add-generic-password", "-a", name, "-s", service,
         "-U", "-w", value, "-D", "wheelscan credential",
         "-j", "Used by the weekly-wheel-scan tools"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise KeychainError(
            f"Could not store {name}: {result.stderr.strip() or result.returncode}"
        )


def delete(name: str, service: str = SERVICE) -> bool:
    """Remove one secret. True when something was removed."""
    if not available():
        return False
    result = subprocess.run(
        [_SECURITY, "delete-generic-password", "-a", name, "-s", service],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0


def load_into_environ(names: tuple[str, ...] = KEYS, service: str = SERVICE) -> list[str]:
    """Copy stored secrets into os.environ. Returns the names that were found.

    Existing environment variables are never overwritten, so an explicit
    export or a CI secret still wins.
    """
    loaded = []
    for name in names:
        if os.environ.get(name):
            continue
        value = get(name, service)
        if value:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def status(names: tuple[str, ...] = KEYS, service: str = SERVICE) -> dict[str, bool]:
    return {name: get(name, service) is not None for name in names}

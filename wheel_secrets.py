#!/usr/bin/env python3
"""Store Alpaca credentials in the macOS Keychain instead of a file.

    python wheel_secrets.py status     # where credentials resolve from
    python wheel_secrets.py store      # prompt and save to the Keychain
    python wheel_secrets.py migrate    # move an existing .env in, then delete it
    python wheel_secrets.py delete     # remove them from the Keychain
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from wheelkit import secrets as keychain
from wheelkit.providers import config_dir, env_search_path, load_credentials


def cmd_status() -> int:
    print(f"Keychain available: {keychain.available()}")
    if keychain.available():
        for name, present in keychain.status().items():
            print(f"  {name:24} {'stored' if present else 'not stored'}")

    print("\nFiles searched:")
    for path in env_search_path():
        mode = ""
        if path.exists():
            bits = path.stat().st_mode & 0o777
            mode = f"  mode {bits:o}" + ("  << readable by others" if bits & 0o077 else "")
        print(f"  {path}{'  EXISTS' if path.exists() else ''}{mode}")

    for key in keychain.KEYS:
        os.environ.pop(key, None)
    print(f"\nCredentials would load from: {load_credentials()}")
    return 0


def cmd_store() -> int:
    if not keychain.available():
        print("The macOS Keychain is not available on this system.", file=sys.stderr)
        return 2
    print("Paste your Alpaca keys. Input is hidden and never echoed.\n")
    for name in keychain.KEYS:
        value = getpass.getpass(f"  {name}: ").strip()
        if not value:
            print(f"\nNo value given for {name}; nothing was changed.", file=sys.stderr)
            return 1
        keychain.put(name, value)
    print("\nStored in the Keychain.")
    print("Delete any leftover .env now:")
    for path in env_search_path():
        if path.exists():
            print(f"  rm {path}")
    return 0


def cmd_migrate() -> int:
    if not keychain.available():
        print("The macOS Keychain is not available on this system.", file=sys.stderr)
        return 2

    source = next((p for p in env_search_path() if p.exists()), None)
    if source is None:
        print("No .env found to migrate. Use 'store' instead.", file=sys.stderr)
        return 1

    values: dict[str, str] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key in keychain.KEYS and value:
            values[key] = value

    missing = [k for k in keychain.KEYS if k not in values]
    if missing:
        print(f"{source} has no value for {', '.join(missing)}.", file=sys.stderr)
        return 1

    for name, value in values.items():
        keychain.put(name, value)
    print(f"Copied {len(values)} secret(s) from {source} into the Keychain.")

    # Overwrite before unlinking so the plaintext does not linger in freed
    # blocks; the file is small enough that this costs nothing.
    try:
        length = source.stat().st_size
        with source.open("r+b") as handle:
            handle.write(b"\0" * length)
            handle.flush()
            os.fsync(handle.fileno())
        source.unlink()
        print(f"Removed {source}.")
    except OSError as exc:
        print(f"Could not remove {source}: {exc}", file=sys.stderr)
        print("Delete it by hand - it still contains your keys.", file=sys.stderr)
        return 1
    return 0


def cmd_delete() -> int:
    removed = [name for name in keychain.KEYS if keychain.delete(name)]
    print(f"Removed {len(removed)} secret(s) from the Keychain.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=("status", "store", "migrate", "delete"),
        nargs="?", default="status",
    )
    args = parser.parse_args()
    return {
        "status": cmd_status, "store": cmd_store,
        "migrate": cmd_migrate, "delete": cmd_delete,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Carry the irreplaceable files between machines.

`git clone` gets you the code. It does not get you any of the state, because
all of it is gitignored - deliberately, since it is position data. Most of it
can be rebuilt. Two things cannot:

* **the archive**, which is the whole point of the evaluation harness. Its
  value is that it accumulates, and the free data tier carries no option
  history, so a lost `archive/` cannot be reconstructed from anywhere. The
  three-month clock for IV rank restarts at zero.
* **the fill log**, which is the only record of what was actually traded and
  the only source of entry dates for the checkpoint.

    python wheel_data.py pack
    python wheel_data.py restore wheelscan-data-2026-09-08.tgz

Credentials are deliberately excluded - see `pack`.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import tarfile
from datetime import date
from pathlib import Path

# Everything worth carrying, and nothing that rebuilds itself cheaply. Paths
# are relative to the repository root.
BUNDLED = (
    "positions.csv",
    "shares.csv",
    "fills.csv",
    "archive",
    "universe",
    # Regenerable in principle. In practice the Nasdaq calendar is fetched one
    # day at a time and has taken over twenty minutes when the feed is slow,
    # so it rides along - it is 32 KB.
    ".earnings_cache.json",
)

# Files that restore merges rather than replaces. Scans are dated, so two
# machines' histories union cleanly; a straight overwrite would silently throw
# one of them away.
MERGE_DIRS = ("archive/scans",)

SECRET_HINT = """
  Credentials are NOT in this bundle, by design.

  Putting Alpaca keys into a plaintext tarball would undo the reason they
  live in the Keychain. Move them yourself, on the old machine:

    security find-generic-password -s wheelscan -a ALPACA_API_KEY_ID -w
    security find-generic-password -s wheelscan -a ALPACA_API_SECRET_KEY -w
    security find-generic-password -s wheelscan -a WHEELSCAN_NTFY_TOPIC -w

  then on the new one: python wheel_secrets.py store
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pack and restore the files a clone does not carry.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    pack = sub.add_parser("pack", help="Bundle the untracked state")
    pack.add_argument("--output", type=Path, help="Target .tgz (default: dated)")
    pack.add_argument("--output-dir", type=Path,
                      help="Write a dated bundle into this directory. For a "
                           "scheduled run, which cannot compose a filename.")
    pack.add_argument("--root", type=Path, default=Path("."))

    restore = sub.add_parser("restore", help="Unpack into this checkout")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--root", type=Path, default=Path("."))
    restore.add_argument("--force", action="store_true",
                         help="Overwrite files that already have content")
    restore.add_argument("--dry-run", action="store_true",
                         help="Say what would happen and change nothing")
    return p.parse_args()


# ---------------------------------------------------------------------
# Describing what is there
# ---------------------------------------------------------------------


def _rows(path: Path) -> int:
    """Data rows in a CSV, ignoring comments and the header."""
    try:
        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeDecodeError):
        return 0
    return max(len(lines) - 1, 0)


def describe(root: Path) -> list[str]:
    """A human summary of the state, used before packing and after restoring.

    Counts rather than sizes: "212 archived candidates" says whether the
    restore worked in a way that "16K" does not.
    """
    out: list[str] = []
    for name in ("positions.csv", "shares.csv", "fills.csv"):
        path = root / name
        if path.exists():
            out.append(f"{name:<22} {_rows(path)} row(s)")

    scans = sorted((root / "archive" / "scans").glob("*.csv"))
    if scans:
        candidates = sum(_rows(p) for p in scans)
        out.append(
            f"{'archive/scans':<22} {len(scans)} scan(s), "
            f"{candidates} candidate(s), {scans[0].stem} to {scans[-1].stem}"
        )

    outcomes = root / "archive" / "outcomes.csv"
    if outcomes.exists():
        out.append(f"{'archive/outcomes.csv':<22} {_rows(outcomes)} resolved")

    iv = root / "archive" / "atm_iv.csv"
    if iv.exists():
        days, symbols = set(), set()
        try:
            with iv.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    days.add(row.get("observed_on"))
                    symbols.add(row.get("symbol"))
        except (OSError, csv.Error):
            pass
        # IV rank needs 60 observations before it will report anything.
        note = "" if len(days) >= 60 else f" - {60 - len(days)} more days to IV rank"
        out.append(
            f"{'archive/atm_iv.csv':<22} {len(days)} day(s), "
            f"{len(symbols)} symbol(s){note}"
        )

    universe = root / "universe" / "symbols.txt"
    if universe.exists():
        kept = len([
            l for l in universe.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")
        ])
        out.append(f"{'universe/symbols.txt':<22} {kept} symbol(s)")
    return out


# ---------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------


def cmd_pack(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    present = [name for name in BUNDLED if (root / name).exists()]
    if not present:
        print(f"Nothing to pack in {root}. Has this checkout ever been run?",
              file=sys.stderr)
        return 1

    name = f"wheelscan-data-{date.today().isoformat()}.tgz"
    if args.output:
        target = args.output
    elif args.output_dir:
        # Created rather than required: on a fresh machine the sync folder may
        # not exist yet, and failing the weekly backup over a missing
        # directory would be the backup failing for the least good reason.
        args.output_dir.expanduser().mkdir(parents=True, exist_ok=True)
        target = args.output_dir.expanduser() / name
    else:
        target = Path(name)
    with tarfile.open(target, "w:gz") as bundle:
        for name in present:
            bundle.add(root / name, arcname=name)

    print(f"Packed {len(present)} item(s) into {target.resolve()} "
          f"({target.stat().st_size / 1024:.0f} KB)\n")
    for line in describe(root):
        print(f"  {line}")

    missing = [name for name in BUNDLED if name not in present]
    if missing:
        print(f"\n  not present, so not packed: {', '.join(missing)}")
    print(SECRET_HINT)
    return 0


# ---------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------


def _has_content(path: Path) -> bool:
    if path.is_dir():
        return any(path.iterdir())
    return path.exists() and path.stat().st_size > 0


def _safe_members(bundle: tarfile.TarFile, root: Path) -> list[tarfile.TarInfo]:
    """Members that stay inside the target directory.

    A tarball is untrusted input even when you made it: an absolute path or a
    `..` traversal would write outside the checkout.
    """
    safe = []
    for member in bundle.getmembers():
        target = (root / member.name).resolve()
        if not str(target).startswith(str(root.resolve())):
            print(f"  refusing {member.name} - escapes the target directory",
                  file=sys.stderr)
            continue
        if member.issym() or member.islnk():
            print(f"  refusing {member.name} - link", file=sys.stderr)
            continue
        safe.append(member)
    return safe


def cmd_restore(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    if not args.archive.exists():
        print(f"No such bundle: {args.archive}", file=sys.stderr)
        return 2

    with tarfile.open(args.archive, "r:gz") as bundle:
        members = _safe_members(bundle, root)
        top = {Path(m.name).parts[0] for m in members}

        # Merge the dated scan files; refuse to clobber anything else.
        conflicts = [
            name for name in sorted(top)
            if _has_content(root / name)
            and not any(name == Path(d).parts[0] for d in MERGE_DIRS)
        ]
        if conflicts and not args.force:
            print("Refusing to overwrite existing data:", file=sys.stderr)
            for name in conflicts:
                print(f"  {name}", file=sys.stderr)
            print("\nMove them aside, or pass --force. archive/scans merges "
                  "either way - dated files from two machines union cleanly.",
                  file=sys.stderr)
            return 1

        if args.dry_run:
            print(f"Would restore {len(members)} item(s) into {root}:")
            for name in sorted(top):
                print(f"  {name}")
            return 0

        # Extract to a staging directory so a merge can be done file by file.
        staging = root / ".wheelscan-restore"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        try:
            for member in members:
                bundle.extract(member, staging)
            merged, replaced = _merge(staging, root, args.force)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    print(f"Restored into {root}: {replaced} file(s) placed, "
          f"{merged} archived scan(s) merged.\n")
    for line in describe(root):
        print(f"  {line}")
    print(SECRET_HINT)
    return 0


def _merge(staging: Path, root: Path, force: bool) -> tuple[int, int]:
    """Copy staged files in, unioning the dated scan directory."""
    merged = replaced = 0
    for source in sorted(staging.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(staging)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        in_merge_dir = any(
            str(relative).startswith(d + "/") for d in MERGE_DIRS
        )
        if in_merge_dir and target.exists():
            continue  # same dated scan already here; keep what is on disk
        if target.exists() and not force and not in_merge_dir:
            continue
        shutil.copy2(source, target)
        if in_merge_dir:
            merged += 1
        else:
            replaced += 1
    return merged, replaced


def main() -> int:
    args = parse_args()
    return {"pack": cmd_pack, "restore": cmd_restore}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

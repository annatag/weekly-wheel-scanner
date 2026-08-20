#!/usr/bin/env python3
"""Rebuild symbols.txt from live broker data.

Read-only. Screens every optionable US equity Alpaca will trade down to the
names that suit a wheel, ranked by real consolidated dollar volume.

    python build_universe.py                          # rebuild symbols.txt
    python build_universe.py --max-symbols 100        # a tighter list
    python build_universe.py --max-price 60           # small sleeve
    python build_universe.py --dry-run                # preview, write nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wheelkit.netio import FetchError
from wheelkit.providers import AlpacaProvider
from wheelkit.universe import (
    DEFAULT_UNIVERSE_PATH,
    UniverseFilters,
    screen,
    write_symbols_file,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build the scan universe from live Alpaca data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--output", type=Path, default=DEFAULT_UNIVERSE_PATH)
    p.add_argument("--min-price", type=float, default=5.0)
    p.add_argument(
        "--max-price",
        type=float,
        default=200.0,
        help="Highest share price. One contract secures 100 shares, so this "
             "is your per-position cash ceiling divided by 100.",
    )
    p.add_argument(
        "--min-dollar-volume",
        type=float,
        default=50_000_000,
        help="Minimum 20-day average consolidated dollar volume",
    )
    p.add_argument("--max-symbols", type=int, default=250,
                   help="Keep this many, ranked by liquidity. 0 keeps all.")
    p.add_argument("--min-realised-vol", type=float, default=0.15,
                   help="Drop names too quiet to pay premium (e.g. SGOV)")
    p.add_argument("--include-leveraged", action="store_true",
                   help="Do not exclude 2x/3x/inverse products (not advised)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the result without writing the file")
    p.add_argument("--show", type=int, default=25,
                   help="How many rows to print")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    filters = UniverseFilters(
        min_price=args.min_price,
        max_price=args.max_price,
        min_dollar_volume=args.min_dollar_volume,
        min_realised_vol=args.min_realised_vol,
        max_symbols=args.max_symbols,
        exclude_leveraged=not args.include_leveraged,
    )

    try:
        provider = AlpacaProvider()
    except FetchError as exc:
        print(f"Could not start Alpaca: {exc}", file=sys.stderr)
        return 2

    try:
        report = screen(provider, filters)
    except FetchError as exc:
        print(f"Screen failed: {exc}", file=sys.stderr)
        return 2

    print(f"\nConsidered {report.considered} assets. Rejected:")
    for reason, count in sorted(report.rejected.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>6}  {reason}")
    print(f"  {len(report.kept):>6}  kept")

    if not report.kept:
        print("\nNothing passed the screen. Loosen --min-dollar-volume or "
              "widen the price band.", file=sys.stderr)
        return 1

    print(f"\nTop {min(args.show, len(report.kept))} by liquidity:")
    print(f"  {'Symbol':<8}{'Price':>10}{'$Vol/day':>12}{'Vol':>8}  Name")
    print(f"  {'-' * 8} {'-' * 9} {'-' * 11} {'-' * 6}  {'-' * 32}")
    for entry in report.kept[: args.show]:
        print(f"  {entry.symbol:<8}${entry.price:>9,.2f}"
              f"{entry.dollar_volume / 1e6:>11,.0f}M"
              f"{entry.realised_vol:>7.0%}  {entry.name[:32]}")

    if args.dry_run:
        print(f"\nDry run: {args.output} not written.")
        return 0

    write_symbols_file(args.output, report, filters)
    print(f"\nWrote {len(report.kept)} symbols to {args.output.resolve()}")
    print("Review the list and delete anything you would not want to own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

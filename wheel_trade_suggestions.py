#!/usr/bin/env python3
"""Re-quote a saved scan and reprice the limit orders.

Read-only. Run this immediately before trading: the scan may be hours or days
old, and an option's bid/ask moves far more than the underlying does. Anything
that no longer holds up is marked WAIT rather than repriced.

    python wheel_trade_suggestions.py
    python wheel_trade_suggestions.py --input wheel_scan_results.csv --top 3
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

from wheelkit.analytics import compute_stats
from wheelkit.earnings import EarningsCalendar
from wheelkit.engine import prepare_context
from wheelkit.netio import FetchError
from wheelkit.providers import get_provider
from wheelkit.report import print_trade_card
from wheelkit.strategy import (
    WheelConfig,
    build_candidates,
    resize_candidate,
    score_candidate,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Refresh saved scan results against the current market.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--provider", choices=("alpaca", "ibkr"), default="alpaca")
    p.add_argument("--input", type=Path, default=Path("wheel_scan_results.csv"))
    p.add_argument("--output", type=Path, default=Path("wheel_trade_suggestions.csv"))
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--max-spread-pct", type=float, default=0.12,
                   help="Mark WAIT above this spread instead of suggesting a price")
    p.add_argument("--earnings-file", type=Path, default=Path("earnings.csv"))
    p.add_argument("--offline-earnings", action="store_true")
    return p.parse_args()


def load_saved(path: Path, top: int) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. Run weekly_wheel_scan.py first."
        )
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{path} has no rows. Run weekly_wheel_scan.py first.")

    required = {"symbol", "expiration", "strike", "right"}
    missing = required.difference(rows[0])
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(sorted(missing))}")
    return rows[:top]


def main() -> int:
    args = parse_args()
    try:
        saved = load_saved(args.input, args.top)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Could not load saved results: {exc}", file=sys.stderr)
        return 2

    try:
        provider = get_provider(args.provider)
    except Exception as exc:
        print(f"Could not start the {args.provider} provider: {exc}", file=sys.stderr)
        return 2

    earnings = EarningsCalendar.build(
        args.earnings_file, offline=args.offline_earnings
    )
    context = prepare_context(provider, earnings)
    today = date.today()

    print(f"Re-quoting {len(saved)} contract(s) from {args.input.resolve()}")
    print(f"As of {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}\n")

    # Permissive except for spread: the contract was already vetted by the
    # scan, so the only question now is whether it is still tradable today.
    cfg = WheelConfig(
        min_cash=0,
        max_cash=10_000_000,
        allow_single_oversize=True,
        min_dte=0,
        max_dte=400,
        min_abs_delta=0.0,
        max_abs_delta=1.0,
        max_spread_pct=args.max_spread_pct,
        min_option_volume=0,
        min_quote_size=0,
        min_credit_per_share=0.01,
        min_avg_dollar_volume=0,
        max_abs_move_5d=1.0,
        min_vrp=0.0,
        min_annualised_return=0.0,
        skip_earnings=False,
    )

    refreshed = []
    for row in saved:
        symbol = row["symbol"].strip().upper()
        right = (row.get("right") or "P").strip().upper()[:1]
        strike = float(row["strike"])
        expiration = datetime.fromisoformat(row["expiration"].strip()).date()
        label = f"{symbol} ${strike:g} {'PUT' if right == 'P' else 'CALL'} {expiration:%b %d}"

        if expiration <= today:
            print(f"WAIT  {label} — already expired.")
            continue

        try:
            bars = provider.daily_bars(symbol, 400)
            spot, _ = provider.spot(symbol)
            stats = compute_stats(bars, spot)
            quotes = provider.option_chain(
                symbol,
                expiry_from=expiration,
                expiry_to=expiration,
                strike_min=strike - 0.01,
                strike_max=strike + 0.01,
                right=right,
            )
        except FetchError as exc:
            print(f"WAIT  {label} — data unavailable ({exc}).")
            continue

        if stats is None or not quotes:
            print(f"WAIT  {label} — the contract no longer quotes.")
            continue

        shares, basis = 0.0, 0.0
        if right == "C":
            getter = getattr(provider, "positions", None)
            if callable(getter):
                try:
                    shares, basis = getter().get(symbol, (0.0, 0.0))
                except FetchError:
                    pass

        candidates = build_candidates(
            symbol, quotes, stats, cfg,
            right=right, today=today,
            earnings_date=earnings.next_date(symbol),
            market_open=context.market_open,
            shares_held=shares or 100.0, cost_basis=basis,
        )
        if not candidates:
            print(f"WAIT  {label} — no longer passes the spread/quote check.")
            continue

        candidate = candidates[0]
        # Keep the position size the scan decided on; only the price is stale.
        try:
            saved_contracts = int(float(row.get("contracts") or 0))
        except ValueError:
            saved_contracts = 0
        if saved_contracts > 0:
            resize_candidate(candidate, saved_contracts)
        score_candidate(candidate, cfg, context.regime_score)
        refreshed.append(candidate)
        print_trade_card(candidate, index=len(refreshed))

    if refreshed:
        from wheelkit.report import write_csv

        write_csv(args.output, refreshed)
        print(f"\nSaved {len(refreshed)} row(s) to {args.output.resolve()}")
    else:
        print("\nNothing is currently tradable from the saved scan.")

    if getattr(provider, "close", None):
        provider.close()
    print("No order was placed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

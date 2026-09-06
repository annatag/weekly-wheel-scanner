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
    p.add_argument("--max-gap", type=float, default=0.05,
                   help="Flag a contract whose underlying has moved this far "
                        "since the scan saved it. Matches the position "
                        "monitor's daily-move alert.")
    p.add_argument("--notify", action="store_true",
                   help="Send the verdict to a banner and a phone. For the "
                        "scheduled morning run, which nobody is watching.")
    p.add_argument("--no-banner", action="store_true")
    p.add_argument("--no-push", action="store_true")
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
        min_credit_pct_of_strike=0.0,
        min_avg_dollar_volume=0,
        max_abs_move_5d=1.0,
        min_vrp=0.0,
        min_annualised_return=0.0,
        skip_earnings=False,
    )

    refreshed: list = []
    # Two separate kinds of "do not fire": the contract stopped being
    # tradable, and the stock moved out from under a still-tradable contract.
    # The second is the one an overnight hold has to check, and repricing
    # alone would hide it - the limits would simply come back different.
    waits: list[tuple[str, str]] = []
    gaps: list[tuple[str, float]] = []

    for row in saved:
        symbol = row["symbol"].strip().upper()
        right = (row.get("right") or "P").strip().upper()[:1]
        strike = float(row["strike"])
        expiration = datetime.fromisoformat(row["expiration"].strip()).date()
        label = f"{symbol} ${strike:g} {'PUT' if right == 'P' else 'CALL'} {expiration:%b %d}"

        if expiration <= today:
            print(f"WAIT  {label} — already expired.")
            waits.append((label, "already expired"))
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
            waits.append((label, "data unavailable"))
            continue

        try:
            saved_spot = float(row.get("spot") or 0)
        except ValueError:
            saved_spot = 0.0
        if saved_spot > 0:
            gap = (spot - saved_spot) / saved_spot
            if abs(gap) >= args.max_gap:
                gaps.append((label, gap))
                print(f"GAP   {label} — underlying {gap:+.1%} since the scan "
                      f"(${saved_spot:,.2f} → ${spot:,.2f}). The contract may "
                      f"still quote; the trade you picked has changed.")

        if stats is None or not quotes:
            print(f"WAIT  {label} — the contract no longer quotes.")
            waits.append((label, "the contract no longer quotes"))
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
            waits.append((label, "no longer passes the spread/quote check"))
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

    verdict = summarise_requote(len(saved), refreshed, waits, gaps)
    print()
    for line in verdict:
        print(f"  {line}")
    if args.notify:
        send_requote(verdict, waits, gaps, args)

    print("\nNo order was placed.")
    # 1 means "read this before you trade", not an error. The scheduled run is
    # unattended, so a clean overnight hold and a stock that gapped 8% must not
    # both exit 0.
    return 1 if (waits or gaps) else 0


def summarise_requote(
    total: int, refreshed: list, waits: list, gaps: list
) -> list[str]:
    """The one-glance verdict, in the order it should be acted on."""
    lines = [f"{len(refreshed)} of {total} still tradable."]
    if gaps:
        lines.append(
            f"{len(gaps)} gapped past the threshold: "
            + "; ".join(f"{label} {pct:+.1%}" for label, pct in gaps)
        )
    if waits:
        lines.append(
            f"{len(waits)} no longer tradable: "
            + "; ".join(f"{label} ({reason})" for label, reason in waits)
        )
    if not gaps and not waits:
        lines.append("Nothing gapped and nothing dropped out overnight.")
    return lines


def send_requote(verdict: list[str], waits: list, gaps: list, args) -> None:
    """Push the verdict, because nobody is watching the 10:30 run."""
    from wheelkit.notify import NotifyConfig, dispatch
    from wheelkit.risk import INFO, URGENT, WARN, Finding

    level = URGENT if gaps else (WARN if waits else INFO)
    findings = [Finding(level, "requote", line) for line in verdict]
    config = NotifyConfig.from_environment(
        banner=not args.no_banner, push=not args.no_push
    )
    results = dispatch([("pre-trade check", findings)], config)
    for channel, ok in results.items():
        if not ok:
            print(f"  (note: {channel} notification did not send)",
                  file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

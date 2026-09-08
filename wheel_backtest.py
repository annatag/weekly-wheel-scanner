#!/usr/bin/env python3
"""Resolve archived scans against what actually happened, and report.

The screen ranks ten candidates a run and you trade one or two. Judged on
trades alone, a real change in win rate takes years to show up. Judged on
every ranked candidate, the same weeks produce ten times the evidence - and
the ones that placed fourth through tenth are the control group that says
whether the ranking put the right contracts on top.

    python wheel_backtest.py resolve      # score matured candidates
    python wheel_backtest.py report       # what the archive says so far

Read-only against the market, and it never touches the live scan. Resolving
answers one question per candidate: where did the underlying close on the
expiry, and would the put have expired worthless.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from wheelkit.archive import (
    DEFAULT_ARCHIVE_DIR,
    read_archived_scans,
    read_outcomes,
    write_outcomes,
)
from wheelkit.netio import FetchError
from wheelkit.providers import AlpacaProvider


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Evaluate archived scans against realised outcomes.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("command", choices=("resolve", "report"))
    p.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    p.add_argument("--min-sample", type=int, default=20,
                   help="Refuse to report a bucket thinner than this")
    return p.parse_args()


def _float(value, default=float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def close_on(provider: AlpacaProvider, symbol: str, day: date,
             cache: dict) -> float:
    """Closing price on a specific day, or NaN. Bars are cached per symbol."""
    if symbol not in cache:
        try:
            cache[symbol] = {b.day: b.close for b in provider.daily_bars(symbol, 400)}
        except FetchError:
            cache[symbol] = {}
    return cache[symbol].get(day, float("nan"))


def cmd_resolve(args: argparse.Namespace) -> int:
    rows = read_archived_scans(args.archive_dir)
    if not rows:
        print(f"No archived scans in {args.archive_dir / 'scans'}. Run the "
              "scanner at least once first.", file=sys.stderr)
        return 1

    already = {
        (r.get("scan_id"), r.get("symbol"), r.get("expiration"), r.get("strike"))
        for r in read_outcomes(args.archive_dir)
    }
    today = date.today()

    try:
        provider = AlpacaProvider()
    except FetchError as exc:
        print(f"Could not start Alpaca: {exc}", file=sys.stderr)
        return 2

    resolved, pending, cache = [], 0, {}
    for row in rows:
        key = (row.get("scan_id"), row.get("symbol"), row.get("expiration"),
               row.get("strike"))
        if key in already:
            continue
        try:
            expiration = date.fromisoformat((row.get("expiration") or "").strip())
        except ValueError:
            continue
        if expiration >= today:
            pending += 1
            continue

        symbol = (row.get("symbol") or "").upper()
        strike = _float(row.get("strike"))
        close = close_on(provider, symbol, expiration, cache)
        if close != close or strike != strike:
            continue

        right = (row.get("right") or "P").upper()[:1]
        itm = close < strike if right == "P" else close > strike
        credit = _float(row.get("mid"))
        contracts = _float(row.get("contracts"), 1.0)

        # Per share: the credit kept, less intrinsic value if it finished in
        # the money. Assignment is not automatically a loss - the credit and
        # the cushion come off it first.
        intrinsic = max(strike - close, 0.0) if right == "P" else max(close - strike, 0.0)
        per_share = credit - intrinsic
        resolved.append({
            "scan_id": row.get("scan_id"), "scanned_on": row.get("scanned_on"),
            "symbol": symbol, "right": right,
            "expiration": expiration.isoformat(), "strike": f"{strike:g}",
            "delta": row.get("delta"), "score": row.get("score"),
            "credit": row.get("credit"), "capital": row.get("capital"),
            "breakeven": row.get("breakeven"),
            "close_at_expiry": f"{close:.4f}",
            "expired_worthless": "no" if itm else "yes",
            "assigned": "yes" if itm else "no",
            "pnl_per_share": f"{per_share:.4f}",
            "pnl": f"{per_share * 100.0 * contracts:.2f}",
            "resolved_on": today.isoformat(),
        })

    combined = read_outcomes(args.archive_dir) + resolved
    path = write_outcomes(combined, directory=args.archive_dir)
    print(f"Resolved {len(resolved)} newly matured candidate(s); "
          f"{pending} still open.")
    print(f"{len(combined)} total outcome(s) in {path}")
    if not resolved and pending:
        print("\nNothing has matured yet. The first useful report is roughly "
              "three weeks after the first archived scan.")
    return 0


def _bucket(value: float, edges: list[float]) -> str:
    for low, high in zip(edges, edges[1:]):
        if low <= value < high:
            return f"{low:.2f}-{high:.2f}"
    return f"{edges[-1]:.2f}+"


def cmd_report(args: argparse.Namespace) -> int:
    outcomes = read_outcomes(args.archive_dir)
    scans = read_archived_scans(args.archive_dir)
    print(f"{len(scans)} archived candidate(s), {len(outcomes)} resolved.\n")
    if not outcomes:
        print("Nothing resolved yet. Run 'resolve' once contracts have expired.")
        return 0

    worthless = sum(1 for o in outcomes if o.get("expired_worthless") == "yes")
    pnls = [_float(o.get("pnl")) for o in outcomes]
    pnls = [p for p in pnls if p == p]
    print(f"  expired worthless : {worthless}/{len(outcomes)} "
          f"({worthless / len(outcomes):.0%})")
    if pnls:
        print(f"  total P/L         : ${sum(pnls):+,.0f}")
        print(f"  median            : ${statistics.median(pnls):+,.2f}")
        losses = [p for p in pnls if p < 0]
        if losses:
            worst = sorted(losses)[:max(1, len(pnls) // 20)]
            print(f"  worst 5% average  : ${sum(worst) / len(worst):+,.0f}")

    # The calibration test: does a 0.20-delta put assign about 20% of the time?
    print("\n  ASSIGNMENT RATE BY DELTA — the calibration test")
    by_delta = defaultdict(list)
    for o in outcomes:
        d = abs(_float(o.get("delta")))
        if d == d:
            by_delta[_bucket(d, [0.10, 0.14, 0.18, 0.22])].append(
                o.get("assigned") == "yes")
    for bucket in sorted(by_delta):
        hits = by_delta[bucket]
        flag = "" if len(hits) >= args.min_sample else "   (thin)"
        print(f"    delta {bucket:<12} {sum(hits):>3}/{len(hits):<4} "
              f"= {sum(hits) / len(hits):>5.0%}{flag}")

    # Does the score rank anything?
    print("\n  OUTCOME BY SCORE — does the ranking predict?")
    scored = [(_float(o.get("score")), o) for o in outcomes]
    scored = [(s, o) for s, o in scored if s == s]
    if len(scored) >= 4:
        scored.sort(key=lambda so: -so[0])
        half = len(scored) // 2
        for label, group in (("top half", scored[:half]), ("bottom half", scored[half:])):
            wins = sum(1 for _, o in group if o.get("expired_worthless") == "yes")
            total_pnl = sum(_float(o.get("pnl"), 0.0) for _, o in group)
            print(f"    {label:<12} {wins}/{len(group)} worthless, "
                  f"${total_pnl:+,.0f}")
        if len(scored) < args.min_sample * 2:
            print(f"    (thin: {len(scored)} resolved, want "
                  f"{args.min_sample * 2}+ before reading anything into this)")
    else:
        print("    not enough resolved candidates yet")
    return 0


def main() -> int:
    args = parse_args()
    return {"resolve": cmd_resolve, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

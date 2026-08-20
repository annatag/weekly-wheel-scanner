#!/usr/bin/env python3
"""Deep-dive one symbol: which strike, which expiry, and at what limit price.

Read-only. Answers the two questions the scanner leaves open for a ticker you
have already chosen — how far out to sell, and how far out of the money.

    python wheel_advise.py AAPL
    python wheel_advise.py NVDA --right call
    python wheel_advise.py SOFI --max-dte 45 --capital 10000
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from wheelkit.analytics import compute_stats
from wheelkit.earnings import EarningsCalendar
from wheelkit.engine import prepare_context, strike_window
from wheelkit.netio import FetchError
from wheelkit.providers import get_provider
from wheelkit.report import (
    RIGHT_LABEL,
    fmt_num,
    print_context,
    print_trade_card,
    print_underlying_summary,
    render_table,
)
from wheelkit.strategy import Candidate, WheelConfig, build_candidates

# Delta rungs shown in the ladder, from conservative to aggressive.
DELTA_RUNGS = (0.10, 0.15, 0.20, 0.25, 0.30)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Strike, expiry and limit-price advice for one symbol.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("symbol", help="Ticker to analyse")
    p.add_argument("--provider", choices=("alpaca", "ibkr"), default="alpaca")
    p.add_argument("--right", choices=("put", "call", "both"), default="both")
    p.add_argument("--min-dte", type=int, default=5)
    p.add_argument("--max-dte", type=int, default=45)
    p.add_argument("--capital", type=float, default=20_000,
                   help="Cash you are willing to secure per put position")
    p.add_argument("--shares", type=float, default=None,
                   help="Shares held, for covered calls. Defaults to the broker position.")
    p.add_argument("--cost-basis", type=float, default=0.0,
                   help="Average cost per share; calls below it are excluded")
    p.add_argument("--target-delta", type=float, default=0.20,
                   help="Delta used to pick the headline recommendation")
    p.add_argument("--earnings-file", type=Path, default=Path("earnings.csv"))
    p.add_argument("--offline-earnings", action="store_true")
    p.add_argument("--allow-earnings", action="store_true")
    return p.parse_args()


def advisor_config(args: argparse.Namespace) -> WheelConfig:
    """Permissive config: the advisor shows the menu, it does not gate it.

    The scanner's job is to reject; here the user has already picked the
    ticker, so filters that would return an empty screen are unhelpful.
    """
    return WheelConfig(
        min_cash=0,
        max_cash=args.capital,
        allow_single_oversize=True,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        min_abs_delta=0.05,
        max_abs_delta=0.45,
        ideal_abs_delta=args.target_delta,
        max_spread_pct=0.35,
        min_option_volume=0,
        min_quote_size=0,
        min_credit_per_share=0.02,
        min_avg_dollar_volume=0,
        max_abs_move_5d=1.0,
        min_vrp=0.0,
        min_annualised_return=0.0,
        skip_earnings=not args.allow_earnings,
        top_n=50,
    )


def nearest_to_delta(candidates: list[Candidate], target: float) -> Candidate | None:
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c.delta) - target))


def print_duration_table(candidates: list[Candidate], target_delta: float, right: str) -> None:
    """Compare expiries at a constant delta so duration is the only variable."""
    by_expiry: dict[date, list[Candidate]] = {}
    for candidate in candidates:
        by_expiry.setdefault(candidate.expiration, []).append(candidate)

    rows = []
    for expiry in sorted(by_expiry):
        best = nearest_to_delta(by_expiry[expiry], target_delta)
        if best is None:
            continue
        rows.append([
            best.expiration.strftime("%a %b %d"),
            str(best.dte),
            f"${best.strike:g}",
            f"{best.delta:+.2f}",
            f"${best.mid:.2f}",
            f"${best.credit:,.0f}",
            fmt_num(best.return_on_capital, ".2%"),
            fmt_num(best.annualised_return, ".0%"),
            f"${best.theta_per_day:+,.0f}",
            fmt_num(best.prob_profit, ".0%"),
            f"{best.score:.0f}",
        ])

    if not rows:
        return
    print(f"\n  DURATION — best {RIGHT_LABEL[right]} near {target_delta:.2f} delta "
          "at each expiry")
    print("  " + render_table(rows, [
        "Expiry", "DTE", "Strike", "Delta", "Mid", "Credit", "ROC", "Ann.",
        "Theta/day", "P(profit)", "Score",
    ]).replace("\n", "\n  "))


def print_strike_ladder(candidates: list[Candidate], expiry: date, right: str) -> None:
    """Show the risk/reward trade-off across strikes for one expiry."""
    same_expiry = [c for c in candidates if c.expiration == expiry]
    if not same_expiry:
        return

    chosen: dict[float, Candidate] = {}
    for rung in DELTA_RUNGS:
        best = nearest_to_delta(same_expiry, rung)
        if best and abs(abs(best.delta) - rung) < 0.05:
            chosen[best.strike] = best

    rows = []
    for candidate in sorted(chosen.values(), key=lambda c: c.strike, reverse=right == "P"):
        rows.append([
            f"${candidate.strike:g}",
            f"{candidate.delta:+.2f}",
            f"{candidate.bid:.2f}/{candidate.ask:.2f}",
            f"${candidate.mid:.2f}",
            f"${candidate.credit:,.0f}",
            f"${candidate.breakeven:.2f}",
            f"{candidate.cushion_pct:+.1%}",
            fmt_num(candidate.cushion_sigmas, ".2f") + "σ",
            fmt_num(candidate.prob_profit, ".0%"),
            fmt_num(candidate.prob_itm, ".0%"),
            fmt_num(candidate.annualised_return, ".0%"),
            f"{candidate.score:.0f}",
        ])

    if not rows:
        return
    print(f"\n  STRIKE LADDER — {RIGHT_LABEL[right]}s expiring {expiry:%B %d, %Y}")
    print("  " + render_table(rows, [
        "Strike", "Delta", "Bid/Ask", "Mid", "Credit", "B/E", "Cushion",
        "σ", "P(profit)", "P(assign)", "Ann.", "Score",
    ]).replace("\n", "\n  "))


def analyse(
    provider, symbol: str, cfg: WheelConfig, context, earnings, right: str,
    *, shares: float, cost_basis: float, target_delta: float,
) -> list[Candidate]:
    today = date.today()
    bars = provider.daily_bars(symbol, 400)
    spot, _ = provider.spot(symbol)
    stats = compute_stats(bars, spot)
    if stats is None:
        raise FetchError(f"not enough price history for {symbol}")

    low, high = strike_window(stats.spot, stats, cfg, right)
    quotes = provider.option_chain(
        symbol,
        expiry_from=today + timedelta(days=cfg.min_dte),
        expiry_to=today + timedelta(days=cfg.max_dte),
        strike_min=low,
        strike_max=high,
        right=right,
    )

    rejects: Counter[str] = Counter()
    candidates = build_candidates(
        symbol, quotes, stats, cfg,
        right=right, today=today,
        earnings_date=earnings.next_date(symbol),
        market_open=context.market_open,
        shares_held=shares, cost_basis=cost_basis,
        stats_counter=rejects,
    )
    from wheelkit.strategy import score_candidate

    for candidate in candidates:
        score_candidate(candidate, cfg, context.regime_score)
    return candidates, rejects


def main() -> int:
    args = parse_args()
    symbol = args.symbol.strip().upper()
    cfg = advisor_config(args)

    try:
        provider = get_provider(args.provider)
    except Exception as exc:
        print(f"Could not start the {args.provider} provider: {exc}", file=sys.stderr)
        return 2

    shares = args.shares
    cost_basis = args.cost_basis
    if shares is None:
        shares = 0.0
        getter = getattr(provider, "positions", None)
        if callable(getter):
            try:
                held = getter().get(symbol)
                if held:
                    shares, broker_basis = held
                    cost_basis = cost_basis or broker_basis
            except FetchError:
                pass

    earnings = EarningsCalendar.build(
        args.earnings_file, horizon_days=args.max_dte + 20,
        offline=args.offline_earnings,
    )
    context = prepare_context(provider, earnings)

    try:
        bars = provider.daily_bars(symbol, 400)
        spot, _ = provider.spot(symbol)
        stats = compute_stats(bars, spot)
    except FetchError as exc:
        print(f"Could not load {symbol}: {exc}", file=sys.stderr)
        return 2
    if stats is None:
        print(f"Not enough price history for {symbol}.", file=sys.stderr)
        return 1

    print("=" * 74)
    print(f"WHEEL ADVISOR — {symbol}")
    print("=" * 74)
    print_context(context)
    print_underlying_summary(symbol, stats, earnings.next_date(symbol))

    rights = ["P", "C"] if args.right == "both" else ["P" if args.right == "put" else "C"]
    exit_code = 0

    for right in rights:
        label = "CASH-SECURED PUTS" if right == "P" else "COVERED CALLS"
        print(f"\n{'-' * 74}\n{label}\n{'-' * 74}")

        if right == "C" and shares < 100:
            print(f"  You hold {shares:g} shares of {symbol}. A covered call needs at "
                  "least 100.\n  Pass --shares to model it anyway.")
            continue

        try:
            candidates, rejects = analyse(
                provider, symbol, cfg, context, earnings, right,
                shares=shares, cost_basis=cost_basis,
                target_delta=args.target_delta,
            )
        except FetchError as exc:
            print(f"  Could not analyse {right}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        if not candidates:
            print("  No tradable contracts found in the requested range.")
            rejects.pop("accepted", None)
            for reason, count in rejects.most_common(6):
                print(f"    {count:>5}  {reason}")
            continue

        print_duration_table(candidates, args.target_delta, right)

        best = max(candidates, key=lambda c: (c.score, c.annualised_return))
        print_strike_ladder(candidates, best.expiration, right)

        print(f"\n  RECOMMENDATION")
        print_trade_card(best)

    if getattr(provider, "close", None):
        provider.close()
    print("\nNo order was placed. Verify the quote in your broker before selling.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Plan a covered call on shares you were assigned.

Read-only. Never places, modifies or closes an order.

    python wheel_covered_call.py CCL --shares 500 --basis 27.34
    python wheel_covered_call.py CCL --assigned-from 28 0.656   # strike, put credit
    python wheel_covered_call.py GM                             # basis from the broker

Assignment usually happens because the stock fell, so the shares often arrive
already underwater. That is the case a plain screen handles worst: every
strike above your basis pays almost nothing, and every strike that pays well
would lock in a loss if called away. This shows both sides of that trade
rather than hiding one of them.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta

from wheelkit.analytics import compute_stats, variance_risk_premium
from wheelkit.earnings import EarningsCalendar
from wheelkit.netio import FetchError
from wheelkit.orders import build_limit_plan, round_to_tick
from wheelkit.positions import load_positions
from wheelkit.pricing import compute_greeks, implied_vol
from wheelkit.providers import AlpacaProvider
from wheelkit.report import fmt_num, render_table
from wheelkit.strategy import Candidate

DAYS_PER_YEAR = 365.0


@dataclass
class CallOption:
    """One covered-call candidate, costed against the share basis."""

    strike: float
    expiration: date
    dte: int
    bid: float
    ask: float
    mid: float
    spread_pct: float
    volume: float
    iv: float
    delta: float
    prob_called: float
    contracts: int
    credit: float  # premium for the whole position
    below_basis: bool
    share_pnl_if_called: float  # gain or loss on the shares alone
    total_if_called: float  # shares plus premium
    static_return: float  # premium / capital, annualised
    called_return: float  # total / capital, annualised


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Plan a covered call on assigned shares.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("symbol")
    p.add_argument("--shares", type=float, help="Shares held (default: from broker)")
    p.add_argument("--basis", type=float, help="Average cost per share")
    p.add_argument(
        "--assigned-from", nargs=2, type=float, metavar=("STRIKE", "PUT_CREDIT"),
        help="Derive the basis from the put that assigned you: strike minus credit",
    )
    p.add_argument("--min-dte", type=int, default=5)
    p.add_argument("--max-dte", type=int, default=60)
    p.add_argument("--target-delta", type=float, default=0.25)
    p.add_argument(
        "--allow-below-basis", action="store_true",
        help="Include strikes that would realise a loss if called away",
    )
    p.add_argument("--earnings-file", default="earnings.csv")
    p.add_argument("--offline-earnings", action="store_true")
    return p.parse_args()


def resolve_position(
    args: argparse.Namespace, provider: AlpacaProvider
) -> tuple[float, float]:
    """Work out shares held and cost basis from flags, then the broker."""
    shares = args.shares or 0.0
    basis = args.basis or 0.0

    if args.assigned_from:
        strike, put_credit = args.assigned_from
        # Assignment cost you the strike, but you kept the put premium, so the
        # effective basis is lower than the strike by exactly that credit.
        basis = strike - put_credit

    if not shares or not basis:
        try:
            held = provider.positions().get(args.symbol.upper())
        except FetchError:
            held = None
        if held:
            shares = shares or held[0]
            basis = basis or held[1]

    return shares, basis


def build_calls(
    provider: AlpacaProvider,
    symbol: str,
    spot: float,
    basis: float,
    contracts: int,
    args: argparse.Namespace,
    today: date,
) -> list[CallOption]:
    quotes = provider.option_chain(
        symbol,
        expiry_from=today + timedelta(days=args.min_dte),
        expiry_to=today + timedelta(days=args.max_dte),
        strike_min=spot * 0.92,
        strike_max=spot * 1.45,
        right="C",
    )

    capital = basis * 100.0 * contracts
    out: list[CallOption] = []
    for q in quotes:
        if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
            continue
        dte = (q.expiration - today).days
        if dte < args.min_dte:
            continue
        horizon = max(dte, 0.5) / DAYS_PER_YEAR
        iv = implied_vol(q.mid, spot, q.strike, horizon, right="C")
        if iv is None:
            continue
        greeks = compute_greeks(spot, q.strike, horizon, iv, q.mid, right="C")
        if greeks is None:
            continue

        credit = q.mid * 100.0 * contracts
        share_pnl = (q.strike - basis) * 100.0 * contracts
        total = share_pnl + credit
        out.append(
            CallOption(
                strike=q.strike, expiration=q.expiration, dte=dte,
                bid=q.bid, ask=q.ask, mid=q.mid, spread_pct=q.spread_pct,
                volume=q.volume, iv=iv, delta=greeks.delta,
                prob_called=greeks.prob_itm, contracts=contracts, credit=credit,
                below_basis=q.strike < basis,
                share_pnl_if_called=share_pnl, total_if_called=total,
                static_return=(credit / capital) * DAYS_PER_YEAR / dte
                if capital > 0 and dte else float("nan"),
                called_return=(total / capital) * DAYS_PER_YEAR / dte
                if capital > 0 and dte else float("nan"),
            )
        )
    return out


def nearest(calls: list[CallOption], target: float) -> CallOption | None:
    return min(calls, key=lambda c: abs(c.delta - target)) if calls else None


def print_ladder(calls: list[CallOption], basis: float, spot: float,
                 expiry: date) -> None:
    same = sorted(
        (c for c in calls if c.expiration == expiry), key=lambda c: c.strike
    )
    if not same:
        return
    rows = []
    for c in same:
        rows.append([
            f"${c.strike:g}" + (" *" if c.below_basis else ""),
            f"{c.delta:+.2f}",
            f"{c.bid:.2f}/{c.ask:.2f}",
            f"${c.credit:,.0f}",
            fmt_num(c.prob_called, ".0%"),
            f"${c.share_pnl_if_called:+,.0f}",
            f"${c.total_if_called:+,.0f}",
            fmt_num(c.static_return, ".0%"),
            fmt_num(c.called_return, ".0%"),
        ])
    print(f"\n  STRIKE LADDER — calls expiring {expiry:%B %d, %Y}")
    print("  " + render_table(rows, [
        "Strike", "Delta", "Bid/Ask", "Credit", "P(called)",
        "Shares if called", "Total if called", "Ann. if kept", "Ann. if called",
    ]).replace("\n", "\n  "))
    if any(c.below_basis for c in same):
        print(f"    * strike is below your ${basis:,.2f} basis — being called "
              "away there realises a loss on the shares")


def print_duration(calls: list[CallOption], target: float) -> None:
    by_expiry: dict[date, list[CallOption]] = {}
    for c in calls:
        by_expiry.setdefault(c.expiration, []).append(c)

    rows = []
    for expiry in sorted(by_expiry):
        best = nearest(by_expiry[expiry], target)
        if best is None:
            continue
        rows.append([
            expiry.strftime("%a %b %d"), str(best.dte), f"${best.strike:g}",
            f"{best.delta:+.2f}", f"${best.credit:,.0f}",
            fmt_num(best.static_return, ".0%"),
            fmt_num(best.prob_called, ".0%"),
            "below basis" if best.below_basis else "",
        ])
    if rows:
        print(f"\n  DURATION — nearest {target:.2f} delta at each expiry")
        print("  " + render_table(rows, [
            "Expiry", "DTE", "Strike", "Delta", "Credit", "Ann. if kept",
            "P(called)", "",
        ]).replace("\n", "\n  "))


def print_repair(calls: list[CallOption], basis: float, spot: float,
                 contracts: int, target: float) -> None:
    """How long premium alone would take to close an underwater gap."""
    gap = (basis - spot) * 100.0 * contracts
    if gap <= 0:
        return
    safe = [c for c in calls if not c.below_basis]
    best = nearest(safe, target) or (nearest(calls, target) if calls else None)
    if best is None or best.credit <= 0:
        return

    cycles = gap / best.credit
    weeks = cycles * best.dte / 7.0
    print(f"\n  RECOVERY MATH")
    print(f"    You are ${gap:,.0f} below basis on {int(contracts * 100)} shares.")
    print(f"    At ${best.credit:,.0f} per {best.dte}-day cycle "
          f"(${best.strike:g} strike, {best.delta:+.2f} delta), premium alone")
    print(f"    closes that gap in about {cycles:.1f} cycles "
          f"— roughly {weeks:.0f} weeks, if the stock goes nowhere.")
    print(f"    That assumes you are never called away and never roll down.")


def main() -> int:
    args = parse_args()
    symbol = args.symbol.upper()

    try:
        provider = AlpacaProvider()
    except FetchError as exc:
        print(f"Could not start Alpaca: {exc}", file=sys.stderr)
        return 2

    shares, basis = resolve_position(args, provider)
    if shares < 100:
        print(f"A covered call needs 100+ shares; found {shares:g} for {symbol}.",
              file=sys.stderr)
        print("Pass --shares to model it anyway.", file=sys.stderr)
        return 1
    if basis <= 0:
        print("No cost basis. Pass --basis, or --assigned-from STRIKE CREDIT.",
              file=sys.stderr)
        return 1

    try:
        bars = provider.daily_bars(symbol, 400)
        spot, _ = provider.spot(symbol)
    except FetchError as exc:
        print(f"Could not load {symbol}: {exc}", file=sys.stderr)
        return 2
    stats = compute_stats(bars, spot)
    if stats is None:
        print(f"Not enough price history for {symbol}.", file=sys.stderr)
        return 1

    contracts = int(shares // 100)
    capital = basis * 100.0 * contracts
    unrealised = (spot - basis) * 100.0 * contracts

    print("=" * 78)
    print(f"COVERED CALL PLAN — {symbol}")
    print("=" * 78)
    print(f"  Holding    {int(contracts * 100)} shares "
          f"({contracts} contract{'s' if contracts > 1 else ''} coverable)")
    print(f"  Basis      ${basis:,.2f} per share  ·  ${capital:,.0f} committed")
    print(f"  Spot       ${spot:,.2f}  ({(spot - basis) / basis:+.1%} vs basis, "
          f"${unrealised:+,.0f} unrealised)")
    print(f"  Trend      {stats.setup} · quarter {fmt_num(stats.move_quarter, '+.1%')} "
          f"· month {fmt_num(stats.move_20d, '+.1%')} · IV vs realised "
          f"{fmt_num(stats.rv20, '.0%')}")

    earnings = EarningsCalendar.build(
        __import__("pathlib").Path(args.earnings_file),
        offline=args.offline_earnings,
    )
    event = earnings.next_date(symbol)
    if event:
        print(f"  ⚠ Earnings {event:%B %d, %Y} — a call through that date "
              "carries gap risk in both directions")

    if spot < basis:
        print(f"\n  UNDERWATER by ${basis - spot:,.2f}/share. Strikes above your "
              "basis pay little;")
        print("  strikes that pay well would realise a loss if called away. Both "
              "are shown.")

    today = date.today()
    try:
        calls = build_calls(provider, symbol, spot, basis, contracts, args, today)
    except FetchError as exc:
        print(f"Could not load the option chain: {exc}", file=sys.stderr)
        return 2
    if not calls:
        print("\n  No tradable call quotes in that range.", file=sys.stderr)
        return 1

    usable = calls if args.allow_below_basis else [c for c in calls if not c.below_basis]
    if not usable:
        print("\n  Every quoted strike sits below your basis. Re-run with "
              "--allow-below-basis")
        print("  to see what they pay, or wait for the shares to recover.")
        usable = calls

    print_duration(usable, args.target_delta)

    pick = nearest(usable, args.target_delta)
    if pick:
        print_ladder(calls, basis, spot, pick.expiration)
        print_repair(calls, basis, spot, contracts, args.target_delta)

        limits = build_limit_plan(
            Candidate(
                symbol=symbol, right="C", occ_symbol="", expiration=pick.expiration,
                dte=pick.dte, strike=pick.strike, spot=spot, bid=pick.bid,
                ask=pick.ask, mid=pick.mid, spread_pct=pick.spread_pct,
                option_volume=pick.volume, open_interest=None, iv=pick.iv,
                delta=pick.delta, theta_per_day=0.0, prob_itm=pick.prob_called,
                prob_profit=1 - pick.prob_called, vrp=float("nan"),
                contracts=contracts, capital=capital, credit=pick.credit,
                breakeven=pick.strike + pick.mid, cushion_pct=float("nan"),
                cushion_sigmas=float("nan"), return_on_capital=float("nan"),
                annualised_return=pick.static_return, trend_score=stats.trend_score,
                avg_dollar_volume=stats.avg_dollar_volume, rv20=stats.rv20,
                move_5d=stats.move_5d, support_20d=stats.support_20d,
                earnings_date=event, quote_age_note="",
            )
        )
        print(f"\n  SUGGESTED")
        print(f"    SELL TO OPEN {contracts} × {symbol} ${pick.strike:g} CALL "
              f"{pick.expiration:%b %d %Y} ({pick.dte} DTE)")
        print(f"    Open at ${limits.open_at:.2f} → ${limits.credit_open:,.0f} · "
              f"mid ${limits.likely_fill:.2f} → ${limits.credit_likely:,.0f} · "
              f"floor ${limits.walk_floor:.2f}")
        print(f"    If it expires worthless you keep ${pick.credit:,.0f} and the "
              "shares.")
        verb = "loss" if pick.total_if_called < 0 else "profit"
        print(f"    If called away at ${pick.strike:g}: "
              f"${pick.total_if_called:+,.0f} total {verb} "
              f"({fmt_num(pick.prob_called, '.0%')} chance).")
        if pick.below_basis:
            print(f"    ⚠ That strike is BELOW your ${basis:,.2f} basis.")

    print("\nNo order was placed. Verify the quote in your broker before selling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

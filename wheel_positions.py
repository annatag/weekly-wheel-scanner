#!/usr/bin/env python3
"""Monitor open option positions and validate trades before you place them.

Read-only. Never places, modifies or closes an order.

    python wheel_positions.py                    # full table plus alerts
    python wheel_positions.py --alerts-only      # silent unless something trips
    python wheel_positions.py --check GM 87 P 2026-08-21 1.09
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from wheelkit.analytics import compute_stats
from wheelkit.earnings import EarningsCalendar
from wheelkit.fills import DEFAULT_FILLS_FILE, entry_date_for, entry_date_index
from wheelkit.netio import FetchError
from wheelkit.notify import NotifyConfig, dispatch
from wheelkit.positions import (
    DEFAULT_POSITIONS_FILE,
    SourceReport,
    OpenOption,
    enrich,
    load_positions,
    occ_symbol,
)
from wheelkit.pricing import compute_greeks, implied_vol
from wheelkit.providers import AlpacaProvider
from wheelkit.report import fmt_num, render_table
from wheelkit.risk import (
    URGENT,
    WARN,
    Finding,
    RiskLimits,
    check_entry,
    check_portfolio,
    check_position,
    size_position,
    worst_level,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Monitor open positions and gate new trades.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", choices=("auto", "ibkr", "alpaca", "csv"),
                   default="auto",
                   help="Where positions come from. auto tries each in turn.")
    p.add_argument("--positions-file", type=Path, default=DEFAULT_POSITIONS_FILE)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--account-value", type=float, default=100_000.0)
    p.add_argument("--time-stop-dte", type=int, default=21,
                   help="Checkpoint at which a position has to justify itself. "
                        "Resolved back to the nearest trading day.")
    p.add_argument("--time-stop-capture", type=float, default=0.35,
                   help="Share of the credit a position must have captured by "
                        "the checkpoint. Below it you get a decision to make; "
                        "above it the trade is working and stays quiet.")
    p.add_argument("--alerts-only", action="store_true",
                   help="Print nothing unless a position trips a threshold")
    p.add_argument("--no-banner", action="store_true",
                   help="Do not post a macOS notification")
    p.add_argument("--no-push", action="store_true",
                   help="Do not send an ntfy push")
    p.add_argument("--notify-test", action="store_true",
                   help="Send a test notification through every channel and exit")
    p.add_argument("--earnings-file", type=Path, default=Path("earnings.csv"))
    p.add_argument("--offline-earnings", action="store_true")
    p.add_argument("--fills-file", type=Path, default=DEFAULT_FILLS_FILE,
                   help="Fill log, used for entry dates the broker does not "
                        "report. Without one the checkpoint falls back to a "
                        "flat DTE, which is early for a short-dated trade.")
    p.add_argument(
        "--check", nargs=5, metavar=("SYMBOL", "STRIKE", "RIGHT", "EXPIRY", "CREDIT"),
        help="Validate a trade before placing it, e.g. --check GM 87 P 2026-08-21 1.09",
    )
    return p.parse_args()


def warn_not_live(report, path: Path) -> None:
    """Say plainly when positions came from a file rather than the broker.

    A quiet fallback is the failure this tool exists to catch. The scanner
    once ran a whole session on its built-in ticker list without saying so,
    and this file carried three wrong entry prices for days. So the source is
    always stated, including under --alerts-only, which is what the scheduled
    job runs.
    """
    print("", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"WARNING: positions came from {path}, NOT your broker.", file=sys.stderr)
    print_attempts(report, stream=sys.stderr)
    print("", file=sys.stderr)
    print("  This file is hand-maintained. Closed or expired positions keep",
          file=sys.stderr)
    print("  alerting until you edit it, and entry prices are whatever was typed.",
          file=sys.stderr)
    print("  Start TWS for live broker data.", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print("", file=sys.stderr)


def run_notify_test(args: argparse.Namespace) -> int:
    """Prove the delivery path works without waiting for a real alert."""
    from wheelkit.notify import NTFY_TOPIC_ENV, banner_available

    config = NotifyConfig.from_environment(
        banner=not args.no_banner, push=not args.no_push
    )
    print(f"  macOS banner : {'available' if banner_available() else 'unavailable'}"
          f"{'' if config.banner else ' (disabled)'}")
    print(f"  ntfy topic   : {config.topic or 'not set'}"
          f"{'' if config.push else ' (disabled)'}")
    if not config.topic:
        print(f"\n  To enable push, pick any hard-to-guess topic name and store it:")
        print(f"    python wheel_secrets.py --set {NTFY_TOPIC_ENV}")
        print(f"  then subscribe to it in the ntfy app or at "
              f"{config.server}/<topic>")

    fake = [("TEST $100P", [Finding(URGENT, "test",
                                    "notification delivery test - no real alert")])]
    results = dispatch(fake, config)
    print()
    for channel, ok in results.items():
        print(f"  {channel:8} {'sent' if ok else 'FAILED'}")
    if not results:
        print("  nothing sent - both channels are disabled or unconfigured")
    return 0


def print_findings(findings: list[Finding], indent: str = "    ") -> None:
    marker = {URGENT: "!!", WARN: " !", "INFO": "  "}
    for finding in findings:
        print(f"{indent}{marker.get(finding.level, '  ')} {finding.message}")


def build_table(positions: list[OpenOption]) -> str:
    rows = []
    for p in positions:
        rows.append([
            f"{p.symbol} ${p.strike:g}{p.right}",
            p.expiration.strftime("%b %d"),
            str(p.dte),
            f"{p.quantity:+d}",
            f"${p.spot:,.2f}" if p.spot == p.spot else "-",
            f"{(p.spot - p.strike) / p.strike:+.1%}" if p.spot == p.spot else "-",
            f"${p.entry_credit:.2f}",
            f"${p.mid:.2f}" if p.mid == p.mid else "-",
            fmt_num(p.delta, "+.2f"),
            fmt_num(p.prob_itm, ".0%"),
            f"${p.unrealised:+,.0f}" if p.unrealised == p.unrealised else "-",
            fmt_num(p.pct_captured, ".0%"),
            "ITM" if p.itm else "OTM",
        ])
    return render_table(rows, [
        "Position", "Expiry", "DTE", "Qty", "Spot", "Moneyness", "Entry",
        "Now", "Delta", "P(assign)", "P&L", "Captured", "",
    ])


def run_check(args: argparse.Namespace, provider: AlpacaProvider,
              positions: list[OpenOption], limits: RiskLimits) -> int:
    """Validate one proposed trade, the gate that was missing at entry."""
    symbol, raw_strike, right, raw_expiry, raw_credit = args.check
    symbol, right = symbol.upper(), right.upper()[:1]
    try:
        strike = float(raw_strike)
        expiration = datetime.fromisoformat(raw_expiry).date()
        credit = float(raw_credit)
    except ValueError as exc:
        print(f"Could not parse the trade: {exc}", file=sys.stderr)
        return 2

    dte = (expiration - date.today()).days
    try:
        bars = provider.daily_bars(symbol, 400)
        spot, _ = provider.spot(symbol)
    except FetchError as exc:
        print(f"Could not load {symbol}: {exc}", file=sys.stderr)
        return 2
    stats = compute_stats(bars, spot)
    if stats is None:
        print(f"Not enough history for {symbol}.", file=sys.stderr)
        return 2

    horizon = max(dte, 0.5) / 365.0
    iv = implied_vol(credit, spot, strike, horizon, right=right)
    greeks = compute_greeks(spot, strike, horizon, iv, credit, right=right) if iv else None
    delta = greeks.delta if greeks else float("nan")

    from wheelkit.analytics import variance_risk_premium

    vrp = variance_risk_premium(iv, stats.rv20, stats.rv60) if iv else float("nan")
    earnings = EarningsCalendar.build(
        args.earnings_file, offline=args.offline_earnings
    )

    print("=" * 74)
    print(f"PRE-TRADE CHECK  ·  SELL {symbol} ${strike:g} "
          f"{'PUT' if right == 'P' else 'CALL'} {expiration:%b %d %Y} @ ${credit:.2f}")
    print("=" * 74)
    print(f"  Spot ${spot:,.2f} · {dte} DTE · delta {fmt_num(delta, '+.2f')} · "
          f"IV {fmt_num(iv, '.0%')} · VRP {fmt_num(vrp, '.2f')} · setup {stats.setup}")

    findings = check_entry(
        symbol=symbol, right=right, strike=strike, spot=spot, delta=delta,
        dte=dte, credit_per_share=credit, spread_pct=0.0, vrp=vrp,
        setup=stats.setup, earnings_date=earnings.next_date(symbol),
        expiration=expiration, limits=limits,
    )

    sizing = size_position(
        strike=strike, spot=spot, iv=iv or stats.rv20, dte=dte, limits=limits
    )
    if sizing:
        print(f"  Suggested size: {sizing.contracts} contract(s), "
              f"${sizing.capital:,.0f} secured")

        # The gate validates risk but said nothing about reward, so judging
        # whether the credit justified the capital meant doing the division
        # by hand - against the stress loss printed on the next line.
        total_credit = credit * 100 * sizing.contracts
        roc = total_credit / sizing.capital if sizing.capital > 0 else float("nan")
        annualised = roc * 365 / dte if dte > 0 else float("nan")
        # Stated as a multiple of the credit rather than a fraction of the
        # loss: "11x the credit" is the direction that means something when
        # you are deciding whether to take the trade.
        loss_multiple = (
            sizing.stress_loss / total_credit if total_credit > 0 else float("nan")
        )
        print(f"    Credit ${total_credit:,.0f} on ${sizing.capital:,.0f} "
              f"= {roc:.2%} over {dte} days = {fmt_num(annualised, '.0%')} annualised")
        print(f"    limited by {sizing.binding_constraint}; a two-sigma "
              f"{sizing.stress_move_pct:.1%} drop would cost "
              f"${sizing.stress_loss:,.0f}"
              f" - {fmt_num(loss_multiple, '.0f')}x the credit")
    else:
        findings.append(Finding(
            URGENT, "unsizeable",
            "no contract count fits the risk budget at this strike",
        ))

    portfolio = check_portfolio(
        [{"symbol": p.symbol, "capital": p.capital} for p in positions],
        proposed={"symbol": symbol,
                  "capital": sizing.capital if sizing else strike * 100},
        limits=limits,
    )

    print()
    if not findings and not portfolio:
        print("  PASS - this trade is inside every limit.")
        return 0

    blocking = [f for f in findings + portfolio if f.level == URGENT]
    print("  BLOCKED" if blocking else "  PASS WITH WARNINGS")
    print_findings(findings + portfolio, indent="    ")
    return 1 if blocking else 0


def report_nothing_found(
    args: argparse.Namespace, report: SourceReport
) -> int:
    """Say whether the book is empty or the monitor simply could not look.

    These are opposite facts and they used to print the same line. A run that
    could not reach TWS reported "No open option positions found. Sources
    tried: auto" - the literal flag, not the per-source outcomes it had
    already collected - which reads exactly like a clean book. It said that
    once while six positions were open.
    """
    if not report.incomplete:
        if not args.alerts_only:
            print("No open option positions found.")
            print_attempts(report, stream=sys.stdout)
            print(f"For a manual list, create {args.positions_file} with "
                  "columns symbol,expiration,strike,right,quantity,"
                  "entry_credit")
        return 0

    # Never silent, even under --alerts-only: this is the case the scheduled
    # job most needs to surface, and a quiet exit 0 is indistinguishable from
    # a quiet all-clear.
    print("", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print("WARNING: found no positions, but not every source answered.",
          file=sys.stderr)
    print("", file=sys.stderr)
    print_attempts(report, stream=sys.stderr)
    print("", file=sys.stderr)
    print("  A source that errored cannot tell an empty book from an",
          file=sys.stderr)
    print("  unreachable one. Start TWS, or re-run with --source csv to",
          file=sys.stderr)
    print("  say plainly which answer you want.", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print("", file=sys.stderr)

    config = NotifyConfig.from_environment(
        banner=not args.no_banner, push=not args.no_push
    )
    dispatch([("position monitor", [Finding(
        URGENT, "sources_unreachable",
        "could not read positions: "
        + "; ".join(f"{name} {reason}" for name, reason in report.failures),
    )])], config)
    return 2


def print_attempts(report: SourceReport, *, stream=None) -> None:
    stream = stream or sys.stdout
    for name, outcome in report.attempts:
        marker = "<- used" if name == report.used else ""
        print(f"    {name:<8} {outcome} {marker}".rstrip(), file=stream)


def main() -> int:
    args = parse_args()
    limits = RiskLimits(
        account_value=args.account_value,
        time_stop_dte=args.time_stop_dte,
        time_stop_min_capture=args.time_stop_capture,
    )

    if args.notify_test:
        return run_notify_test(args)

    try:
        provider = AlpacaProvider()
    except FetchError as exc:
        print(f"Could not start Alpaca: {exc}", file=sys.stderr)
        return 2

    try:
        positions, source_report = load_positions(
            args.source, provider=provider, path=args.positions_file,
            host=args.host, port=args.port,
        )
    except FetchError as exc:
        print(f"Could not read positions: {exc}", file=sys.stderr)
        return 2

    if positions:
        try:
            enrich(provider, positions)
        except FetchError as exc:
            print(f"Could not price positions: {exc}", file=sys.stderr)
            return 2

    if positions and not source_report.is_live:
        warn_not_live(source_report, args.positions_file)

    if args.check:
        return run_check(args, provider, positions, limits)

    if not positions:
        return report_nothing_found(args, source_report)

    # A broker reports what you hold, not when you opened it, and TWS's
    # execution history does not reach back past the current session. So the
    # original length of a trade comes from the fill log, and where it is
    # missing the checkpoint says so rather than quietly using a date that has
    # already passed.
    dated = 0
    index = entry_date_index(args.fills_file)
    for position in positions:
        if position.entry_date is None:
            position.entry_date = entry_date_for(
                index, position.symbol, position.right,
                position.expiration, position.strike,
            )
        dated += position.entry_date is not None

    undated = len(positions) - dated
    if undated and not args.alerts_only:
        print(f"note: {undated} of {len(positions)} position(s) have no entry "
              f"date, so their checkpoint falls back to "
              f"{limits.time_stop_dte} DTE rather than half the trade's life.")
        print(f"      Record fills with wheel_fills.py, or add an entry_date "
              f"column to {args.positions_file}.")
        print()

    for position in positions:
        position.findings = check_position(
            symbol=position.symbol, right=position.right, strike=position.strike,
            spot=position.spot, expiration=position.expiration, dte=position.dte,
            delta=position.delta, entry_credit=position.entry_credit,
            current_mid=position.mid,
            underlying_move_1d=position.underlying_move_1d,
            entry_date=position.entry_date, limits=limits,
        )

    portfolio = check_portfolio(
        [{"symbol": p.symbol, "capital": p.capital} for p in positions],
        limits=limits,
    )
    flagged = [p for p in positions if p.findings]
    everything = [f for p in positions for f in p.findings] + portfolio
    level = worst_level(everything)

    if args.alerts_only and level not in (WARN, URGENT):
        return 0

    if not args.alerts_only:
        total = sum(p.unrealised for p in positions if p.unrealised == p.unrealised)
        committed = sum(p.capital for p in positions if p.capital == p.capital)
        print(f"{len(positions)} open position(s) from {source_report.used} · "
              f"${committed:,.0f} committed · unrealised ${total:+,.0f}")
        print()
        print(build_table(positions))
        print()

    if flagged:
        print("ALERTS")
        for position in flagged:
            print(f"  {position.symbol} ${position.strike:g}{position.right} "
                  f"{position.expiration:%b %d} ({position.dte} DTE)")
            print_findings(position.findings)
    if portfolio:
        print("  PORTFOLIO")
        print_findings(portfolio)
    if flagged:
        config = NotifyConfig.from_environment(
            banner=not args.no_banner, push=not args.no_push
        )
        summary = [
            (f"{p.symbol} ${p.strike:g}{p.right}", p.findings) for p in flagged
        ]
        for channel, ok in dispatch(summary, config).items():
            if not ok:
                print(f"  (note: {channel} notification did not send)",
                      file=sys.stderr)

    if not flagged and not portfolio and not args.alerts_only:
        print("No alerts. Every position is inside its limits.")

    print("\nNothing was traded. This tool only reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

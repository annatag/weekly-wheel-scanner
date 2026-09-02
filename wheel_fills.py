#!/usr/bin/env python3
"""Record what you actually filled, against what the scan suggested.

The scan is graded on its recommendations. Without this, it is graded on
whatever loosely related trade ended up in the account, which is not the same
thing and flatters or damns the model at random.

    python wheel_fills.py record GDX P 2026-09-04 95 1 1.03
    python wheel_fills.py close  GDX P 2026-09-04 95 --debit 0.31
    python wheel_fills.py close  GDX P 2026-09-04 95 --expired
    python wheel_fills.py list
    python wheel_fills.py report

Nothing here talks to a broker. It writes fills.csv and reads it back.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from wheelkit.fills import (
    DEFAULT_FILLS_FILE,
    Fill,
    append_fill,
    best_match,
    compute_drift,
    find_open,
    load_fills,
    read_suggestions,
    save_fills,
    scan_date_of,
)

DEFAULT_SCAN_FILE = Path("wheel_scan_results.csv")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Log option fills against the scan that suggested them.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--fills-file", type=Path, default=DEFAULT_FILLS_FILE)
    sub = p.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Log an entry")
    rec.add_argument("symbol")
    rec.add_argument("right", choices=("P", "C", "p", "c"))
    rec.add_argument("expiration", help="YYYY-MM-DD")
    rec.add_argument("strike", type=float)
    rec.add_argument("contracts", type=int)
    rec.add_argument("credit", type=float, help="Credit per share you received")
    rec.add_argument("--scan-file", type=Path, default=DEFAULT_SCAN_FILE,
                     help="Scan this trade came from. Its numbers are copied "
                          "into the row, so a later scan cannot overwrite them.")
    rec.add_argument("--scan-date", help="YYYY-MM-DD; defaults to the scan "
                                         "file's modification date")
    rec.add_argument("--no-scan", action="store_true",
                     help="An entry with no scan behind it. Logged, not graded.")
    rec.add_argument("--date", help="Fill date, YYYY-MM-DD (default: today)")
    rec.add_argument("--note", default="")

    cls = sub.add_parser("close", help="Record how an entry ended")
    cls.add_argument("symbol")
    cls.add_argument("right", choices=("P", "C", "p", "c"))
    cls.add_argument("expiration", help="YYYY-MM-DD")
    cls.add_argument("strike", type=float)
    cls.add_argument("--debit", type=float, default=None,
                     help="Per-share price paid to buy it back")
    cls.add_argument("--expired", action="store_true",
                     help="Expired worthless; the whole credit is kept")
    cls.add_argument("--assigned", action="store_true")
    cls.add_argument("--rolled", action="store_true")
    cls.add_argument("--date", help="Close date, YYYY-MM-DD (default: today)")
    cls.add_argument("--note", default="")

    sub.add_parser("list", help="Every fill on file")
    rep = sub.add_parser("report", help="How closely fills tracked the scan")
    rep.add_argument("--since", help="Only fills recorded on or after YYYY-MM-DD")
    return p.parse_args()


def _date_or_today(raw: str | None) -> date:
    return date.fromisoformat(raw) if raw else date.today()


def cmd_record(args: argparse.Namespace) -> int:
    symbol = args.symbol.upper()
    right = args.right.upper()
    expiration = date.fromisoformat(args.expiration)

    fill = Fill(
        recorded_at=_date_or_today(args.date),
        symbol=symbol, right=right, expiration=expiration,
        strike=args.strike, contracts=abs(args.contracts),
        fill_credit=args.credit, note=args.note,
    )

    if not args.no_scan:
        if not args.scan_file.exists():
            print(f"No scan file at {args.scan_file}. Pass --scan-file, or "
                  "--no-scan to log this as an ungraded trade.", file=sys.stderr)
            return 2
        match = best_match(read_suggestions(args.scan_file), symbol, right,
                           args.strike)
        if match is None:
            print(f"{symbol} {right} is not in {args.scan_file}. This trade did "
                  "not come from that scan; log it with --no-scan, or point "
                  "--scan-file at the run it did come from.", file=sys.stderr)
            return 2
        fill.scan_file = str(args.scan_file)
        fill.scan_date = (
            date.fromisoformat(args.scan_date) if args.scan_date
            else scan_date_of(args.scan_file)
        )
        fill.suggested_expiration = match.expiration
        fill.suggested_strike = match.strike
        fill.suggested_mid = match.mid
        fill.suggested_limit_likely = match.limit_likely
        fill.suggested_delta = match.delta
        fill.suggested_dte = match.dte
        fill.suggested_score = match.score
        fill.drift = compute_drift(fill)

    append_fill(fill, args.fills_file)

    print(f"Recorded {symbol} ${fill.strike:g}{right} {expiration:%b %d} "
          f"x{fill.contracts} at ${fill.fill_credit:.2f} "
          f"(${fill.credit_received:,.0f} credit)")
    if not fill.scan_file:
        print("  No scan attached - this one is logged but not graded.")
    elif fill.drift:
        print(f"  Drifted from the {fill.scan_date} scan: {fill.drift}")
        print("  Graded separately: the model did not recommend this contract.")
    else:
        print(f"  Matches the {fill.scan_date} scan (score "
              f"{fill.suggested_score:g}, delta {abs(fill.suggested_delta):.2f}).")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    fills = load_fills(args.fills_file)
    expiration = date.fromisoformat(args.expiration)
    fill = find_open(fills, args.symbol, expiration, args.strike,
                     args.right.upper())
    if fill is None:
        print(f"No open fill for {args.symbol.upper()} ${args.strike:g}"
              f"{args.right.upper()} {expiration}. Run 'list' to see what is "
              "on file.", file=sys.stderr)
        return 2

    if args.expired:
        fill.outcome, fill.close_debit = "expired", 0.0
    elif args.assigned:
        fill.outcome = "assigned"
        fill.close_debit = args.debit if args.debit is not None else 0.0
    elif args.rolled:
        fill.outcome = "rolled"
        fill.close_debit = args.debit if args.debit is not None else float("nan")
    elif args.debit is not None:
        fill.outcome, fill.close_debit = "closed", args.debit
    else:
        print("Say how it ended: --debit PRICE, --expired, --assigned or "
              "--rolled.", file=sys.stderr)
        return 2

    fill.closed_on = _date_or_today(args.date)
    if args.note:
        fill.note = f"{fill.note}; {args.note}".strip("; ")
    save_fills(fills, args.fills_file)

    print(f"{fill.symbol} ${fill.strike:g}{fill.right} {fill.expiration:%b %d} "
          f"{fill.outcome} on {fill.closed_on:%b %d}: "
          f"${fill.realised:+,.0f} ({fill.captured_pct:.0%} of the credit kept)")
    if fill.outcome == "assigned":
        print("  That is the option leg only. The shares are now the position - "
              "add them to shares.csv and the rest of the P/L happens there.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    fills = load_fills(args.fills_file)
    if not fills:
        print(f"No fills recorded in {args.fills_file}.")
        return 0
    print(f"{'ENTERED':<11}{'CONTRACT':<24}{'QTY':>4}{'CREDIT':>8}"
          f"{'STATUS':>10}{'P/L':>9}  SCAN")
    for fill in fills:
        contract = (f"{fill.symbol} ${fill.strike:g}{fill.right} "
                    f"{fill.expiration:%b %d}")
        pl = "" if fill.realised != fill.realised else f"{fill.realised:+,.0f}"
        if fill.drift:
            scan = f"drifted ({fill.scan_date})"
        elif fill.scan_file:
            scan = f"matched ({fill.scan_date})"
        else:
            scan = "no scan"
        print(f"{fill.recorded_at.isoformat():<11}{contract:<24}"
              f"{fill.contracts:>4}{fill.fill_credit:>8.2f}"
              f"{fill.outcome or 'open':>10}{pl:>9}  {scan}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    fills = load_fills(args.fills_file)
    if args.since:
        cutoff = date.fromisoformat(args.since)
        fills = [f for f in fills if f.recorded_at >= cutoff]
    if not fills:
        print("Nothing to report.")
        return 0

    matched = [f for f in fills if f.matches_suggestion]
    drifted = [f for f in fills if f.scan_file and f.drift]
    unscanned = [f for f in fills if not f.scan_file]

    print(f"{len(fills)} fill(s): {len(matched)} as suggested, "
          f"{len(drifted)} drifted, {len(unscanned)} with no scan attached.")

    def summarise(label: str, group: list[Fill]) -> None:
        closed = [f for f in group if f.outcome and f.realised == f.realised]
        if not closed:
            print(f"  {label:<14} no closed trades yet")
            return
        total = sum(f.realised for f in closed)
        wins = sum(1 for f in closed if f.realised > 0)
        capture = sum(f.captured_pct for f in closed) / len(closed)
        print(f"  {label:<14} {len(closed):>2} closed  "
              f"${total:>+9,.0f}  {wins}/{len(closed)} green  "
              f"{capture:.0%} of credit kept on average")

    print()
    summarise("as suggested", matched)
    summarise("drifted", drifted)
    summarise("no scan", unscanned)

    if drifted:
        print("\nWhere the fills left the recommendation:")
        for fill in drifted:
            print(f"  {fill.symbol} ${fill.strike:g}{fill.right} "
                  f"{fill.expiration:%b %d} - {fill.drift}")

    if not matched:
        print("\nNo fill has matched a suggestion yet, so nothing here grades "
              "the scanner. That is the finding.")
    return 0


def main() -> int:
    args = parse_args()
    return {
        "record": cmd_record, "close": cmd_close,
        "list": cmd_list, "report": cmd_report,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Scan a universe for the best cash-secured puts or covered calls to sell.

Read-only: this script fetches market data and never places, modifies or
cancels an order.

    python weekly_wheel_scan.py                     # cash-secured puts
    python weekly_wheel_scan.py --right call        # covered calls you can write
    python weekly_wheel_scan.py --symbols AAPL,AMD --top 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wheelkit.earnings import EarningsCalendar
from wheelkit.finviz import fetch_fundamentals, parse_filters
from wheelkit.engine import run_scan
from wheelkit.netio import FetchError
from wheelkit.providers import AlpacaProvider, get_provider
from wheelkit.report import (
    print_context,
    print_diagnostics,
    print_scan_table,
    print_trade_card,
    write_csv,
)
from wheelkit.strategy import WheelConfig
from wheelkit.universe import (
    DEFAULT_UNIVERSE_PATH,
    build_universe_file,
    read_symbols_file,
)

DEFAULT_SYMBOLS = [
    "AAPL", "AMD", "AMZN", "BAC", "C", "CSCO", "CVX", "DAL", "DIS", "F",
    "GDX", "GM", "GOOGL", "HOOD", "INTC", "IWM", "JPM", "KO", "MARA", "MU",
    "NKE", "PFE", "PLTR", "PYPL", "QCOM", "RIVN", "SOFI", "T", "UBER", "VZ",
    "WFC", "XLF", "XOM",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rank cash-secured puts or covered calls worth selling.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--provider", choices=("alpaca", "ibkr"), default="alpaca")
    p.add_argument("--right", choices=("put", "call"), default="put")
    p.add_argument("--symbols", help="Comma-separated tickers")
    p.add_argument("--symbols-file", type=Path, default=DEFAULT_UNIVERSE_PATH)
    p.add_argument("--no-auto-build", action="store_true",
                   help="Do not build the universe when the file is missing")
    p.add_argument("--earnings-file", type=Path, default=Path("earnings.csv"))
    p.add_argument("--output", type=Path, default=Path("wheel_scan_results.csv"))
    p.add_argument("--top", type=int, default=5)
    p.add_argument(
        "--sort", choices=("score", "return", "credit"), default="score",
        help="score = risk-adjusted rank; return = highest annualised; "
             "credit = largest dollar premium",
    )

    p.add_argument("--min-cash", type=float, default=3_000)
    p.add_argument("--max-cash", type=float, default=20_000)
    p.add_argument("--min-dte", type=int, default=7)
    p.add_argument("--max-dte", type=int, default=21)
    p.add_argument("--min-delta", type=float, default=0.10)
    p.add_argument("--max-delta", type=float, default=0.32)
    p.add_argument("--max-spread-pct", type=float, default=0.12)
    p.add_argument("--min-vrp", type=float, default=1.0,
                   help="Minimum implied/realised volatility ratio")
    p.add_argument("--min-annualised", type=float, default=0.12)
    p.add_argument("--min-dollar-volume", type=float, default=50_000_000)

    p.add_argument("--allow-earnings", action="store_true",
                   help="Do not exclude expiries that span an earnings report")
    p.add_argument("--require-uptrend", action="store_true",
                   help="Only sell puts on names trading above their 50-day average")
    p.add_argument("--require-pullback", action="store_true",
                   help="Only names up over the quarter but down over the month")
    p.add_argument("--allow-falling-knife", action="store_true",
                   help="Do not exclude names down over both the quarter and month")

    p.add_argument("--finviz-url",
                   help="Finviz screener URL. Its matches become the universe "
                        "and supply P/E and PEG for the ownership gate.")
    p.add_argument("--finviz-max-rows", type=int, default=200)
    p.add_argument("--max-pe", type=float,
                   help="Reject stocks above this P/E (needs --finviz-url)")
    p.add_argument("--max-peg", type=float,
                   help="Reject stocks above this PEG (needs --finviz-url)")
    p.add_argument("--require-fundamentals", action="store_true",
                   help="Reject names with no P/E or PEG rather than passing them")
    p.add_argument("--allow-duplicate-symbols", action="store_true",
                   help="Permit several strikes on the same ticker in the top N")
    p.add_argument("--offline-earnings", action="store_true",
                   help="Skip the earnings feed and use earnings.csv only")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def _rebuild_command(path: Path) -> str:
    """The exact command that regenerates this universe file."""
    if path == DEFAULT_UNIVERSE_PATH:
        return "python build_universe.py"
    return f"python build_universe.py --output {path}"


def warn_using_builtin_list(path: Path, reason: str) -> None:
    """Say loudly that the scan is about to run on the fallback list.

    Falling back silently once cost a whole session of wrong results: the
    scanner reported on 33 built-in tickers while every conclusion was being
    drawn as though it had screened the full universe.
    """
    print("", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"WARNING: {reason}", file=sys.stderr)
    print(
        f"Falling back to the {len(DEFAULT_SYMBOLS)} built-in tickers, which is "
        "NOT your screened universe.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("  Rebuild it with:", file=sys.stderr)
    print(f"      {_rebuild_command(path)}", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print("", file=sys.stderr)


def load_symbols(args: argparse.Namespace) -> list[str]:
    """Explicit list, then the universe file, building it if it is missing."""
    if args.symbols:
        return sorted(
            {
                s.strip().upper()
                for s in args.symbols.split(",")
                if s.strip() and not s.lstrip().startswith("#")
            }
        )

    path = args.symbols_file
    symbols = read_symbols_file(path) if path else []
    if symbols:
        return symbols

    if not path:
        return sorted(DEFAULT_SYMBOLS)

    if args.no_auto_build:
        warn_using_builtin_list(path, f"{path} does not exist.")
        return sorted(DEFAULT_SYMBOLS)

    print(f"{path} is missing. Building the universe first...")
    try:
        built = build_universe_file(AlpacaProvider(), path)
    except (FetchError, Exception) as exc:
        warn_using_builtin_list(path, f"could not build the universe: {exc}")
        return sorted(DEFAULT_SYMBOLS)

    print(f"Built {len(built)} symbols into {path}.\n")
    return sorted(built)


def main() -> int:
    args = parse_args()
    right = "P" if args.right == "put" else "C"

    cfg = WheelConfig(
        min_cash=args.min_cash,
        max_cash=args.max_cash,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        min_abs_delta=args.min_delta,
        max_abs_delta=args.max_delta,
        max_spread_pct=args.max_spread_pct,
        min_vrp=args.min_vrp,
        min_annualised_return=args.min_annualised,
        min_avg_dollar_volume=args.min_dollar_volume,
        skip_earnings=not args.allow_earnings,
        require_above_sma50=args.require_uptrend,
        require_pullback=args.require_pullback,
        exclude_falling_knife=not args.allow_falling_knife,
        max_pe=args.max_pe,
        max_peg=args.max_peg,
        require_fundamentals=args.require_fundamentals,
        top_n=args.top,
        one_per_symbol=not args.allow_duplicate_symbols,
        sort_by=args.sort,
    )

    try:
        provider = get_provider(args.provider)
    except (FetchError, Exception) as exc:
        print(f"Could not start the {args.provider} provider: {exc}", file=sys.stderr)
        return 2

    fundamentals: dict[str, dict[str, float | None]] = {}
    if args.finviz_url:
        try:
            filters = parse_filters(args.finviz_url)
            print("Fetching the Finviz screen...")
            fundamentals = fetch_fundamentals(
                filters, max_rows=args.finviz_max_rows
            )
        except (ValueError, FetchError) as exc:
            print(f"Could not read the Finviz screen: {exc}", file=sys.stderr)
            return 2
        if not fundamentals:
            print("The Finviz screen returned no stocks.", file=sys.stderr)
            return 1
        symbols = sorted(fundamentals)
        print(f"Universe: {len(symbols)} stock(s) from Finviz\n")
    else:
        symbols = load_symbols(args)

    positions: dict[str, tuple[float, float]] = {}
    if right == "C":
        getter = getattr(provider, "positions", None)
        if callable(getter):
            try:
                positions = getter()
            except FetchError as exc:
                print(f"Could not read positions: {exc}", file=sys.stderr)
        if not positions:
            print(
                "No equity positions found, so there are no covered calls to "
                "write. Sell puts first, or add shares to the account.",
                file=sys.stderr,
            )
            return 1

        # One contract covers 100 shares, so anything smaller cannot be
        # written against. Say so explicitly rather than letting the scan end
        # with a generic "nothing matched".
        coverable = {s: v for s, v in positions.items() if v[0] >= 100}
        if not coverable:
            largest = sorted(positions.items(), key=lambda kv: -kv[1][0])[:5]
            print(
                f"None of your {len(positions)} equity positions reaches the "
                "100 shares a covered call requires.",
                file=sys.stderr,
            )
            print("Largest holdings:", file=sys.stderr)
            for symbol, (shares, basis) in largest:
                print(f"  {symbol:<6} {shares:>10.2f} shares @ ${basis:,.2f}",
                      file=sys.stderr)
            return 1
        positions = coverable
        symbols = sorted(positions)

    print(f"Scanning {len(symbols)} symbol(s) for "
          f"{'cash-secured puts' if right == 'P' else 'covered calls'} "
          f"via {args.provider}...\n")

    earnings = EarningsCalendar.build(
        args.earnings_file, horizon_days=args.max_dte + 20,
        offline=args.offline_earnings,
    )

    candidates, rejects, skipped, context = run_scan(
        provider, symbols, cfg, earnings,
        right=right, positions=positions, fundamentals=fundamentals,
        verbose=not args.quiet,
    )

    print()
    print_context(context)
    if not args.quiet:
        print_diagnostics(rejects, skipped)

    print_scan_table(candidates)
    for i, candidate in enumerate(candidates, 1):
        print_trade_card(candidate, index=i)

    write_csv(args.output, candidates)
    print(f"\nSaved {len(candidates)} row(s) to {args.output.resolve()}")
    print("No order was placed. Re-check the bid/ask in your broker before selling.")

    if getattr(provider, "close", None):
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

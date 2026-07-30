#!/usr/bin/env python3
"""Manual IBKR cash-secured put scanner.

Read-only: this script requests market/contract/historical data and NEVER places orders.
Requires TWS or IB Gateway running with API access enabled.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from ib_async import IB, Option, Stock, util

DEFAULT_SYMBOLS = [
    "AAPL", "AMD", "AMZN", "BAC", "C", "COIN", "DIS", "F", "GDX", "GM",
    "GOOGL", "HOOD", "INTC", "IWM", "JPM", "META", "MSFT", "MU", "NVDA",
    "PLTR", "PYPL", "QCOM", "QQQ", "RIVN", "SHOP", "SOFI", "SPY", "T",
    "TSLA", "UBER", "XLF",
]

MARKET_DATA_TYPE_LABELS = {
    1: "LIVE",
    2: "FROZEN",
    3: "DELAYED",
    4: "DELAYED_FROZEN",
}


@dataclass(frozen=True)
class Config:
    min_cash: float = 7_000
    max_cash: float = 15_000
    min_dte: int = 7
    max_dte: int = 14
    min_abs_delta: float = 0.15
    max_abs_delta: float = 0.20
    max_five_day_move: float = 0.05
    min_underlying_avg_volume: int = 1_000_000
    min_open_interest: int = 250
    min_option_volume: int = 10
    max_spread_pct: float = 0.10
    top_n: int = 5
    snapshot_wait_seconds: float = 3.0


@dataclass
class Candidate:
    symbol: str
    underlying_price: float
    underlying_data_type: str
    option_data_type: str
    expiration: str
    dte: int
    strike: float
    bid: float
    ask: float
    midpoint: float
    delta: float
    iv: float
    open_interest: int
    option_volume: int
    cash_required: float
    premium: float
    breakeven: float
    return_on_cash: float
    annualized_return: float
    five_day_move: float
    avg_stock_volume: float
    support_20d: float
    score: float = 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scan IBKR live or delayed data for 7â€“14 DTE cash-secured puts."
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--port",
        type=int,
        default=7497,
        help="7497 TWS paper; 7496 TWS live; 4002 Gateway paper; 4001 Gateway live",
    )
    p.add_argument("--client-id", type=int, default=21)
    p.add_argument(
        "--market-data",
        choices=("auto", "live", "delayed"),
        default="auto",
        help=(
            "auto (default): live when subscribed, delayed otherwise; "
            "live: require live data; delayed: allow delayed data"
        ),
    )
    p.add_argument(
        "--symbols",
        help="Comma-separated tickers. Defaults to built-in liquid universe.",
    )
    p.add_argument(
        "--symbols-file",
        type=Path,
        help="Text file with one ticker per line.",
    )
    p.add_argument(
        "--earnings-file",
        type=Path,
        default=Path("earnings.csv"),
        help="CSV: symbol,earnings_date",
    )
    p.add_argument("--output", type=Path, default=Path("wheel_scan_results.csv"))
    p.add_argument("--top", type=int, default=5)
    return p.parse_args()


def load_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        values = args.symbols.split(",")
    elif args.symbols_file and args.symbols_file.exists():
        values = args.symbols_file.read_text().splitlines()
    else:
        values = DEFAULT_SYMBOLS
    return sorted(
        {
            s.strip().upper()
            for s in values
            if s.strip() and not s.lstrip().startswith("#")
        }
    )


def load_earnings(path: Path) -> dict[str, date]:
    result: dict[str, date] = {}

    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            symbol = row.get("symbol")
            earnings_date = row.get("earnings_date")

            if not symbol or not earnings_date:
                continue

            symbol = symbol.strip()
            earnings_date = earnings_date.strip()

            if not symbol or not earnings_date or symbol.startswith("#"):
                continue

            result[symbol.upper()] = date.fromisoformat(earnings_date)

    return result


def valid_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def first_valid(*values: object, default: float = math.nan) -> float:
    for value in values:
        if valid_number(value) and float(value) > 0:
            return float(value)
    return default


def market_data_label(value: object) -> str:
    """Return the actual data type reported by IBKR for a ticker."""
    try:
        market_data_type = int(value)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return MARKET_DATA_TYPE_LABELS.get(
        market_data_type, f"UNKNOWN_{market_data_type}"
    )


def configure_market_data(ib: IB, mode: str) -> int:
    """Configure market data before any quote requests.

    IBKR type 3 means "use live data when permission exists, otherwise delayed."
    IBKR does not let a subscribed user force an older delayed quote.
    """
    requested_type = 1 if mode == "live" else 3
    ib.reqMarketDataType(requested_type)
    return requested_type


def data_annotation(candidate: Candidate) -> str:
    if candidate.underlying_data_type == candidate.option_data_type:
        return candidate.option_data_type
    return f"{candidate.underlying_data_type}/{candidate.option_data_type}"


def get_stock_context(
    ib: IB, symbol: str
) -> tuple[Stock, float, str, float, float, float] | None:
    stock = Stock(symbol, "SMART", "USD")
    qualified = ib.qualifyContracts(stock)
    if not qualified:
        return None
    stock = qualified[0]

    ticker = ib.reqMktData(
        stock, "", snapshot=True, regulatorySnapshot=False
    )
    ib.sleep(2)
    spot = first_valid(
        ticker.marketPrice(), ticker.last, ticker.close, ticker.bid, ticker.ask
    )
    if not valid_number(spot):
        return None
    underlying_data_type = market_data_label(
        getattr(ticker, "marketDataType", None)
    )

    bars = ib.reqHistoricalData(
        stock,
        endDateTime="",
        durationStr="35 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
        keepUpToDate=False,
    )
    if len(bars) < 7:
        return None

    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume) for b in bars[-20:] if valid_number(b.volume)]
    five_day_move = closes[-1] / closes[-6] - 1
    avg_volume = sum(volumes) / len(volumes) if volumes else 0
    support_20d = min(float(b.low) for b in bars[-20:])
    return (
        stock,
        spot,
        underlying_data_type,
        five_day_move,
        avg_volume,
        support_20d,
    )


def select_chain(ib: IB, stock: Stock):
    chains = ib.reqSecDefOptParams(
        stock.symbol, "", stock.secType, stock.conId
    )
    smart = [
        c for c in chains if c.exchange == "SMART" and c.multiplier == "100"
    ]
    return smart[0] if smart else (chains[0] if chains else None)


def score_candidate(c: Candidate, cfg: Config) -> float:
    # 25 quality proxy: underlying liquidity + ETF/large-cap bias via volume.
    quality = min(
        100,
        45 + 12 * math.log10(max(c.avg_stock_volume, 1) / 1_000_000 + 1),
    )

    # 20 premium/risk: reward annualized return, but cap extreme values.
    premium_risk = min(
        100, max(0, c.annualized_return / 0.30 * 100)
    )

    # 15 liquidity: OI, option volume and spread.
    spread_pct = (
        (c.ask - c.bid) / c.midpoint if c.midpoint > 0 else 1
    )
    liq = min(
        100,
        35 * min(c.open_interest / 1500, 1)
        + 25 * min(c.option_volume / 300, 1)
        + 40 * max(0, 1 - spread_pct / cfg.max_spread_pct),
    )

    # 15 IV attractiveness: moderate/high IV earns more; very high IV is capped.
    iv_score = min(100, max(0, (c.iv - 0.15) / 0.45 * 100))

    # 15 technical support: favor breakeven below the 20-day low.
    cushion_to_support = (
        c.support_20d - c.breakeven
    ) / c.underlying_price
    technical = min(100, max(0, 60 + cushion_to_support * 1000))

    # 10 market conditions proxy: neutral; replace with VIX if subscribed.
    market = 60

    return round(
        0.25 * quality
        + 0.20 * premium_risk
        + 0.15 * liq
        + 0.15 * iv_score
        + 0.15 * technical
        + 0.10 * market,
        1,
    )


def scan_symbol(
    ib: IB,
    symbol: str,
    earnings: dict[str, date],
    cfg: Config,
) -> list[Candidate]:
    context = get_stock_context(ib, symbol)
    if context is None:
        return []
    (
        stock,
        spot,
        underlying_data_type,
        five_day_move,
        avg_volume,
        support_20d,
    ) = context

    if avg_volume < cfg.min_underlying_avg_volume:
        return []
    if abs(five_day_move) > cfg.max_five_day_move:
        return []

    chain = select_chain(ib, stock)
    if chain is None:
        return []

    today = date.today()
    expirations = []
    for exp in sorted(chain.expirations):
        exp_date = datetime.strptime(exp, "%Y%m%d").date()
        dte = (exp_date - today).days
        if cfg.min_dte <= dte <= cfg.max_dte:
            expirations.append((exp, exp_date, dte))

    results: list[Candidate] = []
    min_strike, max_strike = cfg.min_cash / 100, cfg.max_cash / 100
    # Keep requests manageable by screening strikes using spot/cash bounds first.
    strikes = sorted(
        s
        for s in chain.strikes
        if min_strike <= s <= max_strike and s < spot
    )
    strikes = [s for s in strikes if 0.65 * spot <= s <= 0.99 * spot]

    for exp, exp_date, dte in expirations:
        earnings_date = earnings.get(symbol)
        if earnings_date and today <= earnings_date <= exp_date:
            continue

        contracts = [
            Option(
                symbol,
                exp,
                strike,
                "P",
                "SMART",
                tradingClass=chain.tradingClass,
            )
            for strike in strikes
        ]
        qualified = ib.qualifyContracts(*contracts)
        if not qualified:
            continue

        # Snapshot market data with OI (101) and option volume (100).
        tickers = [
            ib.reqMktData(
                contract,
                "100,101",
                snapshot=True,
                regulatorySnapshot=False,
            )
            for contract in qualified
        ]
        ib.sleep(cfg.snapshot_wait_seconds)

        for contract, ticker in zip(qualified, tickers):
            bid = first_valid(ticker.bid)
            ask = first_valid(ticker.ask)
            if not (
                valid_number(bid)
                and valid_number(ask)
                and ask >= bid
                and bid > 0
            ):
                continue
            midpoint = (bid + ask) / 2
            spread_pct = (ask - bid) / midpoint
            if spread_pct > cfg.max_spread_pct:
                continue

            comp = (
                ticker.modelGreeks
                or ticker.bidGreeks
                or ticker.askGreeks
                or ticker.lastGreeks
            )
            if comp is None or not valid_number(comp.delta):
                continue
            delta = float(comp.delta)
            if not (
                cfg.min_abs_delta <= abs(delta) <= cfg.max_abs_delta
            ):
                continue

            iv = (
                float(comp.impliedVol)
                if valid_number(comp.impliedVol)
                else math.nan
            )
            oi = int(
                first_valid(
                    getattr(ticker, "putOpenInterest", None), default=0
                )
            )
            opt_volume = int(
                first_valid(
                    getattr(ticker, "putVolume", None),
                    ticker.volume,
                    default=0,
                )
            )
            if (
                oi < cfg.min_open_interest
                or opt_volume < cfg.min_option_volume
            ):
                continue

            cash = contract.strike * 100
            premium = midpoint * 100
            breakeven = contract.strike - midpoint
            roc = premium / cash
            annualized = roc * 365 / dte

            candidate = Candidate(
                symbol=symbol,
                underlying_price=spot,
                underlying_data_type=underlying_data_type,
                option_data_type=market_data_label(
                    getattr(ticker, "marketDataType", None)
                ),
                expiration=exp_date.isoformat(),
                dte=dte,
                strike=float(contract.strike),
                bid=bid,
                ask=ask,
                midpoint=midpoint,
                delta=delta,
                iv=iv,
                open_interest=oi,
                option_volume=opt_volume,
                cash_required=cash,
                premium=premium,
                breakeven=breakeven,
                return_on_cash=roc,
                annualized_return=annualized,
                five_day_move=five_day_move,
                avg_stock_volume=avg_volume,
                support_20d=support_20d,
            )
            candidate.score = score_candidate(candidate, cfg)
            results.append(candidate)

    return results


def print_results(rows: Iterable[Candidate]) -> None:
    rows = list(rows)
    if not rows:
        print("No candidates met every filter. Do not lower standards automatically.")
        return
    headers = [
        "#",
        "Score",
        "Ticker",
        "Data",
        "Exp",
        "DTE",
        "Strike",
        "Bid/Ask",
        "Delta",
        "Premium",
        "Cash",
        "ROC",
        "Ann.",
        "B/E",
        "5d move",
        "OI",
        "Vol",
    ]
    table = []
    for i, c in enumerate(rows, 1):
        table.append(
            [
                i,
                f"{c.score:.1f}",
                c.symbol,
                data_annotation(c),
                c.expiration,
                c.dte,
                f"${c.strike:.2f}",
                f"${c.bid:.2f}/${c.ask:.2f}",
                f"{c.delta:.3f}",
                f"${c.premium:.0f}",
                f"${c.cash_required:,.0f}",
                f"{c.return_on_cash:.2%}",
                f"{c.annualized_return:.1%}",
                f"${c.breakeven:.2f}",
                f"{c.five_day_move:+.2%}",
                c.open_interest,
                c.option_volume,
            ]
        )
    print(util.df(table, headers=headers).to_string(index=False))


def write_csv(path: Path, rows: list[Candidate]) -> None:
    fieldnames = list(Candidate.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def main() -> int:
    args = parse_args()
    cfg = Config(top_n=args.top)
    symbols = load_symbols(args)
    earnings = load_earnings(args.earnings_file)

    ib = IB()
    try:
        ib.connect(
            args.host,
            args.port,
            clientId=args.client_id,
            readonly=True,
            timeout=10,
        )
    except Exception as exc:
        print(f"Could not connect to IBKR: {exc}", file=sys.stderr)
        print(
            "Open TWS/IB Gateway, enable API socket clients, and verify host/port.",
            file=sys.stderr,
        )
        return 2

    requested_type = configure_market_data(ib, args.market_data)
    requested_label = MARKET_DATA_TYPE_LABELS[requested_type]
    print(
        f"Connected. Market data mode: {args.market_data.upper()} "
        f"(IBKR request: {requested_label})."
    )
    if args.market_data == "auto":
        print("AUTO uses LIVE data when available and DELAYED data otherwise.")
    elif args.market_data == "delayed":
        print(
            "IBKR returns LIVE data when you have a subscription; "
            "the Data column shows the actual type received."
        )
    print(
        f"Scanning {len(symbols)} symbols at "
        f"{datetime.now().astimezone().isoformat(timespec='seconds')}..."
    )

    all_candidates: list[Candidate] = []
    try:
        for idx, symbol in enumerate(symbols, 1):
            print(f"[{idx}/{len(symbols)}] {symbol}", flush=True)
            try:
                all_candidates.extend(
                    scan_symbol(ib, symbol, earnings, cfg)
                )
            except Exception as exc:
                print(f"  skipped: {exc}", file=sys.stderr)
            time.sleep(0.15)
    finally:
        ib.disconnect()

    ranked = sorted(
        all_candidates,
        key=lambda c: (c.score, c.annualized_return),
        reverse=True,
    )[: cfg.top_n]
    print_results(ranked)
    write_csv(args.output, ranked)
    print(f"\nSaved {len(ranked)} result(s) to {args.output.resolve()}")
    print(
        "Verify bid/ask, Greeks, earnings, and buying power in TWS "
        "immediately before trading."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
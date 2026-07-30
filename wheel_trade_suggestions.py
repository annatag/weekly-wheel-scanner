#!/usr/bin/env python3
"""Refresh the first scanner's top put contracts and suggest limit-credit orders.

Read-only: this script requests IBKR contract and market data and NEVER places,
modifies, or cancels an order. It expects wheel_scan_results.csv produced by
weekly_wheel_scan.py in the same repository.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from ib_async import IB, Option

from weekly_wheel_scan import (
    MARKET_DATA_TYPE_LABELS,
    configure_market_data,
    market_data_label,
    valid_number,
)


@dataclass(frozen=True)
class ScanRow:
    rank: int
    symbol: str
    expiration: str
    strike: float
    scanner_score: float | None


@dataclass
class Recommendation:
    rank: int
    status: str
    reason: str
    symbol: str
    action: str
    outlook: str
    expiration: str
    dte: int
    strike: float
    quantity: int
    data_type: str
    current_bid: float | None = None
    current_ask: float | None = None
    midpoint: float | None = None
    spread_pct: float | None = None
    suggested_limit_credit: float | None = None
    maximum_premium_limit: float | None = None
    estimated_total_credit: float | None = None
    cash_to_secure: float | None = None
    breakeven: float | None = None
    return_on_cash: float | None = None
    annualized_return: float | None = None
    scanner_score: float | None = None
    quoted_at: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh the first scanner's top five cash-secured puts and print "
            "read-only SELL TO OPEN limit-credit suggestions."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=7497,
        help="7497 TWS paper; 7496 TWS live; 4002 Gateway paper; 4001 Gateway live",
    )
    parser.add_argument("--client-id", type=int, default=22)
    parser.add_argument(
        "--market-data",
        choices=("auto", "live", "delayed"),
        default="auto",
        help=(
            "auto (default): live when subscribed, delayed/frozen otherwise; "
            "live: require live data; delayed: allow delayed data"
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("wheel_scan_results.csv"),
        help="CSV produced by weekly_wheel_scan.py",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("wheel_trade_suggestions.csv"),
    )
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument(
        "--quantity",
        type=int,
        default=1,
        help="Contracts per suggestion; one contract controls 100 shares",
    )
    parser.add_argument(
        "--quote-wait-seconds",
        type=float,
        default=6.0,
        help="Seconds to sample IBKR streaming quotes",
    )
    parser.add_argument(
        "--ask-share",
        type=float,
        default=0.75,
        help=(
            "Suggested credit's position from bid to ask. Default 0.75 favors "
            "premium while improving fill odds versus starting at the full ask."
        ),
    )
    parser.add_argument(
        "--max-spread-pct",
        type=float,
        default=0.10,
        help="Print WAIT instead of an order suggestion above this spread",
    )
    args = parser.parse_args()

    if args.top < 1:
        parser.error("--top must be at least 1")
    if args.quantity < 1:
        parser.error("--quantity must be at least 1")
    if args.quote_wait_seconds <= 0:
        parser.error("--quote-wait-seconds must be positive")
    if not 0.5 <= args.ask_share <= 1:
        parser.error("--ask-share must be between 0.5 and 1.0")
    if args.max_spread_pct <= 0:
        parser.error("--max-spread-pct must be positive")
    return args


def parse_expiration(value: str) -> date:
    value = value.strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise ValueError(f"unsupported expiration {value!r}")


def optional_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def load_scan_rows(path: Path, top: int) -> list[ScanRow]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; run weekly_wheel_scan.py first"
        )

    rows: list[ScanRow] = []
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        required = {"symbol", "expiration", "strike"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{path} is missing required column(s): {', '.join(sorted(missing))}"
            )

        for line_number, row in enumerate(reader, 2):
            if len(rows) >= top:
                break
            try:
                symbol = (row.get("symbol") or "").strip().upper()
                expiration = parse_expiration(
                    row.get("expiration") or ""
                ).isoformat()
                strike = float(row.get("strike") or "")
            except ValueError as exc:
                raise ValueError(
                    f"invalid candidate on CSV line {line_number}: {exc}"
                ) from exc
            if not symbol or not valid_number(strike) or strike <= 0:
                raise ValueError(
                    f"invalid symbol or strike on CSV line {line_number}"
                )
            rows.append(
                ScanRow(
                    rank=len(rows) + 1,
                    symbol=symbol,
                    expiration=expiration,
                    strike=strike,
                    scanner_score=optional_float(row.get("score")),
                )
            )
    return rows


def exact_put_contract(ib: IB, row: ScanRow):
    """Resolve the precise standard put already selected by the first scanner."""
    expiration = parse_expiration(row.expiration).strftime("%Y%m%d")
    template = Option(
        row.symbol,
        expiration,
        row.strike,
        "P",
        "SMART",
        multiplier="100",
        currency="USD",
    )
    details = ib.reqContractDetails(template)
    matches = []
    for detail in details:
        contract = detail.contract
        contract_expiration = str(
            getattr(contract, "lastTradeDateOrContractMonth", "")
        )
        multiplier = str(getattr(contract, "multiplier", ""))
        if (
            getattr(contract, "secType", None) == "OPT"
            and getattr(contract, "right", None) == "P"
            and contract_expiration.startswith(expiration)
            and math.isclose(float(contract.strike), row.strike)
            and multiplier == "100"
        ):
            matches.append(detail)
    if not matches:
        return None

    # Prefer the unadjusted trading class when IBKR returns more than one match.
    detail = min(
        matches,
        key=lambda item: (
            getattr(item.contract, "tradingClass", "") != row.symbol,
            getattr(item.contract, "conId", 0),
        ),
    )
    min_tick = optional_float(getattr(detail, "minTick", None)) or 0.01
    return detail.contract, min_tick


def ceil_to_tick(price: float, tick: float) -> float:
    price_decimal = Decimal(str(price))
    tick_decimal = Decimal(str(tick))
    units = (price_decimal / tick_decimal).to_integral_value(
        rounding=ROUND_CEILING
    )
    return float(units * tick_decimal)


def waiting_recommendation(
    row: ScanRow,
    quantity: int,
    reason: str,
    quoted_at: str,
    data_type: str = "UNKNOWN",
) -> Recommendation:
    expiration = parse_expiration(row.expiration)
    return Recommendation(
        rank=row.rank,
        status="WAIT",
        reason=reason,
        symbol=row.symbol,
        action="NO ORDER",
        outlook="BULLISH/NEUTRAL",
        expiration=row.expiration,
        dte=(expiration - date.today()).days,
        strike=row.strike,
        quantity=quantity,
        data_type=data_type,
        scanner_score=row.scanner_score,
        quoted_at=quoted_at,
    )


def make_recommendation(
    row: ScanRow,
    ticker,
    min_tick: float,
    quantity: int,
    ask_share: float,
    max_spread_pct: float,
    quoted_at: str,
) -> Recommendation:
    data_type = market_data_label(
        getattr(ticker, "marketDataType", None)
    )
    bid = optional_float(getattr(ticker, "bid", None))
    ask = optional_float(getattr(ticker, "ask", None))
    if (
        bid is None
        or ask is None
        or bid <= 0
        or ask <= 0
        or ask < bid
    ):
        return waiting_recommendation(
            row,
            quantity,
            "Current option bid/ask unavailable; retry during U.S. option hours.",
            quoted_at,
            data_type,
        )

    midpoint = (bid + ask) / 2
    spread_pct = (ask - bid) / midpoint if midpoint > 0 else math.inf
    if spread_pct > max_spread_pct:
        recommendation = waiting_recommendation(
            row,
            quantity,
            (
                f"Current spread is {spread_pct:.1%}, above the "
                f"{max_spread_pct:.1%} safety limit."
            ),
            quoted_at,
            data_type,
        )
        recommendation.current_bid = bid
        recommendation.current_ask = ask
        recommendation.midpoint = midpoint
        recommendation.spread_pct = spread_pct
        return recommendation

    raw_limit = bid + ask_share * (ask - bid)
    suggested_limit = min(ask, ceil_to_tick(raw_limit, min_tick))
    expiration = parse_expiration(row.expiration)
    dte = (expiration - date.today()).days
    if dte <= 0:
        return waiting_recommendation(
            row,
            quantity,
            "The selected contract has expired or expires today.",
            quoted_at,
            data_type,
        )

    total_credit = suggested_limit * 100 * quantity
    cash_to_secure = row.strike * 100 * quantity
    breakeven = row.strike - suggested_limit
    return_on_cash = total_credit / cash_to_secure
    annualized_return = return_on_cash * 365 / dte
    return Recommendation(
        rank=row.rank,
        status="SUGGESTION",
        symbol=row.symbol,
        action="SELL TO OPEN PUT",
        outlook="BULLISH/NEUTRAL",
        expiration=row.expiration,
        dte=dte,
        strike=row.strike,
        quantity=quantity,
        data_type=data_type,
        current_bid=bid,
        current_ask=ask,
        midpoint=midpoint,
        spread_pct=spread_pct,
        suggested_limit_credit=suggested_limit,
        maximum_premium_limit=ask,
        estimated_total_credit=total_credit,
        cash_to_secure=cash_to_secure,
        breakeven=breakeven,
        return_on_cash=return_on_cash,
        annualized_return=annualized_return,
        scanner_score=row.scanner_score,
        quoted_at=quoted_at,
        reason=(
            f"Limit is {ask_share:.0%} from bid toward ask; start at the ask "
            "if you are willing to wait for a higher credit."
        ),
    )


def expiration_label(value: str) -> str:
    expiration = parse_expiration(value)
    return f"{expiration:%B} {expiration.day}, {expiration.year}"


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def print_recommendations(rows: list[Recommendation]) -> None:
    print("\nTop contract recommendations:")
    for item in rows:
        score = (
            ""
            if item.scanner_score is None
            else f" | Scanner score {item.scanner_score:.1f}"
        )
        print(f"\n#{item.rank} {item.symbol}{score}")
        print(
            f"  Contract: {item.symbol} ${item.strike:g} PUT, "
            f"expiration {expiration_label(item.expiration)} ({item.dte} DTE)"
        )
        if item.current_bid is not None and item.current_ask is not None:
            print(
                f"  Current bid/ask: {money(item.current_bid)} / "
                f"{money(item.current_ask)} [{item.data_type}]"
            )
        if item.status != "SUGGESTION":
            print(f"  WAIT — {item.reason}")
            continue

        print(
            f"  SUGGESTED ORDER: SELL TO OPEN {item.quantity} "
            f"{item.symbol} ${item.strike:g} PUT @ "
            f"{money(item.suggested_limit_credit)} LIMIT CREDIT, DAY"
        )
        print(
            f"  Direction: BULLISH/NEUTRAL | Estimated credit: "
            f"{money(item.estimated_total_credit)} | Cash to secure: "
            f"{money(item.cash_to_secure)}"
        )
        print(
            f"  Breakeven: {money(item.breakeven)} | Return on cash: "
            f"{item.return_on_cash:.2%} | Annualized: "
            f"{item.annualized_return:.1%}"
        )
        print(
            f"  Premium plan: patient starting limit "
            f"{money(item.maximum_premium_limit)} (current ask); "
            f"calculated target {money(item.suggested_limit_credit)}; "
            f"midpoint reference {money(item.midpoint)}."
        )


def write_csv(path: Path, rows: list[Recommendation]) -> None:
    fieldnames = list(Recommendation.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def main() -> int:
    args = parse_args()
    try:
        scan_rows = load_scan_rows(args.input, args.top)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Could not load scanner results: {exc}", file=sys.stderr)
        return 2
    if not scan_rows:
        print(
            f"{args.input} contains no candidates. Run the first scanner and "
            "confirm that it produced at least one result.",
            file=sys.stderr,
        )
        return 1

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
    print(
        f"Connected. Market data mode: {args.market_data.upper()} "
        f"(IBKR request: {MARKET_DATA_TYPE_LABELS[requested_type]})."
    )
    print(
        f"Refreshing {len(scan_rows)} exact put contract(s) from "
        f"{args.input.resolve()}..."
    )

    quoted_at = datetime.now().astimezone().isoformat(timespec="seconds")
    recommendations: list[Recommendation] = []
    active_quotes = []
    try:
        for row in scan_rows:
            try:
                resolved = exact_put_contract(ib, row)
            except Exception as exc:
                recommendations.append(
                    waiting_recommendation(
                        row,
                        args.quantity,
                        f"IBKR contract lookup failed: {exc}",
                        quoted_at,
                    )
                )
                continue
            if resolved is None:
                recommendations.append(
                    waiting_recommendation(
                        row,
                        args.quantity,
                        "IBKR could not resolve this exact put contract.",
                        quoted_at,
                    )
                )
                continue
            contract, min_tick = resolved
            ticker = ib.reqMktData(
                contract,
                "101",
                snapshot=False,
                regulatorySnapshot=False,
            )
            active_quotes.append((row, contract, ticker, min_tick))

        if active_quotes:
            ib.sleep(args.quote_wait_seconds)

        for row, contract, ticker, min_tick in active_quotes:
            recommendations.append(
                make_recommendation(
                    row,
                    ticker,
                    min_tick,
                    args.quantity,
                    args.ask_share,
                    args.max_spread_pct,
                    quoted_at,
                )
            )
    finally:
        for _, contract, _, _ in active_quotes:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass
        ib.disconnect()

    recommendations.sort(key=lambda item: item.rank)
    print_recommendations(recommendations)
    write_csv(args.output, recommendations)
    print(f"\nSaved {len(recommendations)} row(s) to {args.output.resolve()}")

    delayed = {
        item.data_type
        for item in recommendations
        if item.data_type in {"DELAYED", "DELAYED_FROZEN", "FROZEN"}
    }
    if delayed:
        print(
            "WARNING: At least one quote is delayed or frozen. Refresh the "
            "exact bid/ask in TWS before entering any limit price."
        )
    print(
        "This script did not place an order. A short put can be assigned, "
        "requiring purchase of 100 shares per contract at the strike."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
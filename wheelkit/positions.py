"""Read open option positions and price them live.

Positions can come from IBKR, from Alpaca, or from a CSV. The CSV matters:
IBKR only reports while TWS is running, and a scheduled monitor that silently
reports nothing whenever TWS happens to be closed is worse than no monitor.

Pricing always comes from Alpaca regardless of where the positions came from,
because the IBKR account carries no market-data entitlement. Greeks are
computed from the quoted mid.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .netio import FetchError, get_json
from .pricing import compute_greeks, implied_vol
from .providers import ALPACA_DATA_URL, AlpacaProvider, parse_occ

DEFAULT_POSITIONS_FILE = Path("positions.csv")


def occ_symbol(symbol: str, expiration: date, right: str, strike: float) -> str:
    """Build an OCC contract symbol: root + YYMMDD + P/C + strike x1000."""
    return (
        f"{symbol.upper()}{expiration:%y%m%d}{right.upper()}"
        f"{int(round(strike * 1000)):08d}"
    )


@dataclass
class OpenOption:
    symbol: str
    occ: str
    right: str
    strike: float
    expiration: date
    quantity: int  # negative is short
    entry_credit: float  # per share, positive for a credit received
    source: str = "csv"
    sector: str | None = None

    # Filled in by enrich().
    spot: float = float("nan")
    bid: float = float("nan")
    ask: float = float("nan")
    mid: float = float("nan")
    iv: float = float("nan")
    delta: float = float("nan")
    prob_itm: float = float("nan")
    dte: int = 0
    unrealised: float = float("nan")
    pct_captured: float = float("nan")
    underlying_move_1d: float = float("nan")
    capital: float = float("nan")
    findings: list = field(default_factory=list)

    @property
    def contracts(self) -> int:
        return abs(self.quantity)

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def itm(self) -> bool:
        if self.spot != self.spot:
            return False
        return (
            self.spot < self.strike if self.right == "P" else self.spot > self.strike
        )


def read_positions_csv(path: Path) -> list[OpenOption]:
    """Columns: symbol,expiration,strike,right,quantity,entry_credit[,sector]."""
    if not path.exists():
        return []
    out: list[OpenOption] = []
    # Strip comments before parsing: DictReader would otherwise take a leading
    # "# ..." line as the header row and silently yield nothing usable.
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if lines:
        for row in csv.DictReader(lines):
                symbol = (row.get("symbol") or "").strip().upper()
                if not symbol or symbol.startswith("#"):
                    continue
                try:
                    expiration = datetime.fromisoformat(
                        (row.get("expiration") or "").strip()
                    ).date()
                    strike = float(row["strike"])
                    quantity = int(float(row["quantity"]))
                    entry = float(row.get("entry_credit") or 0)
                except (KeyError, ValueError):
                    continue
                right = (row.get("right") or "P").strip().upper()[:1]
                out.append(
                    OpenOption(
                        symbol=symbol,
                        occ=occ_symbol(symbol, expiration, right, strike),
                        right=right,
                        strike=strike,
                        expiration=expiration,
                        quantity=quantity,
                        entry_credit=abs(entry),
                        source="csv",
                        sector=(row.get("sector") or "").strip() or None,
                    )
                )
    return out


def read_positions_alpaca(provider: AlpacaProvider) -> list[OpenOption]:
    """Option positions from the Alpaca trading account."""
    from .providers import ALPACA_TRADE_URL

    payload = get_json(f"{ALPACA_TRADE_URL}/v2/positions", headers=provider._headers)
    out: list[OpenOption] = []
    for row in payload or []:
        if row.get("asset_class") != "us_option":
            continue
        parsed = parse_occ(str(row.get("symbol", "")))
        if not parsed:
            continue
        root, expiration, right, strike = parsed
        quantity = int(float(row.get("qty") or 0))
        entry = abs(float(row.get("avg_entry_price") or 0))
        out.append(
            OpenOption(
                symbol=root, occ=str(row["symbol"]), right=right, strike=strike,
                expiration=expiration, quantity=quantity,
                entry_credit=entry, source="alpaca",
            )
        )
    return out


def read_positions_ibkr(
    host: str = "127.0.0.1", port: int = 7497, client_id: int = 24
) -> list[OpenOption]:
    """Option positions from TWS. Needs no market-data entitlement."""
    from ib_async import IB

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, readonly=True, timeout=12)
    except Exception as exc:
        raise FetchError(
            f"Could not reach TWS on {host}:{port} ({exc}). Start TWS, or use "
            "--source csv."
        ) from exc

    out: list[OpenOption] = []
    try:
        for item in ib.positions():
            contract = item.contract
            if getattr(contract, "secType", "") != "OPT":
                continue
            raw = str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8]
            try:
                expiration = datetime.strptime(raw, "%Y%m%d").date()
            except ValueError:
                continue
            strike = float(getattr(contract, "strike", 0) or 0)
            right = str(getattr(contract, "right", "P"))[:1].upper()
            quantity = int(item.position)
            if not quantity or strike <= 0:
                continue
            # avgCost is per contract for options; normalise to per share.
            entry = abs(float(item.avgCost or 0)) / 100.0
            out.append(
                OpenOption(
                    symbol=str(contract.symbol).upper(),
                    occ=occ_symbol(str(contract.symbol), expiration, right, strike),
                    right=right, strike=strike, expiration=expiration,
                    quantity=quantity, entry_credit=entry, source="ibkr",
                )
            )
    finally:
        if ib.isConnected():
            ib.disconnect()
    return out


def _option_quotes(
    provider: AlpacaProvider, occ_symbols: list[str]
) -> dict[str, dict]:
    if not occ_symbols:
        return {}
    quotes: dict[str, dict] = {}
    # Keep URLs short enough to avoid server-side truncation.
    for start in range(0, len(occ_symbols), 50):
        batch = occ_symbols[start: start + 50]
        payload = get_json(
            f"{ALPACA_DATA_URL}/v1beta1/options/snapshots",
            params={"symbols": ",".join(batch), "feed": provider.option_feed},
            headers=provider._headers,
        )
        quotes.update(payload.get("snapshots") or {})
    return quotes


def enrich(
    provider: AlpacaProvider,
    positions: list[OpenOption],
    *,
    risk_free_rate: float = 0.04,
    today: date | None = None,
) -> list[OpenOption]:
    """Attach live quotes, greeks and P&L to each position."""
    today = today or date.today()
    if not positions:
        return positions

    quotes = _option_quotes(provider, [p.occ for p in positions])
    spot_cache: dict[str, tuple[float, float]] = {}

    for position in positions:
        if position.symbol not in spot_cache:
            try:
                spot, _ = provider.spot(position.symbol)
                bars = provider.daily_bars(position.symbol, 10)
                move = (
                    bars[-1].close / bars[-2].close - 1
                    if len(bars) >= 2 and bars[-2].close > 0
                    else float("nan")
                )
            except FetchError:
                spot, move = float("nan"), float("nan")
            spot_cache[position.symbol] = (spot, move)
        position.spot, position.underlying_move_1d = spot_cache[position.symbol]

        quote = (quotes.get(position.occ) or {}).get("latestQuote") or {}
        bid = float(quote.get("bp") or 0)
        ask = float(quote.get("ap") or 0)
        position.bid, position.ask = bid, ask
        position.mid = (bid + ask) / 2 if bid > 0 and ask > 0 else ask or bid

        position.dte = max((position.expiration - today).days, 0)
        # Expiry day still carries intraday risk, so never price at zero time.
        horizon = max(position.dte, 0.5) / 365.0

        if position.mid > 0 and position.spot == position.spot:
            iv = implied_vol(
                position.mid, position.spot, position.strike, horizon,
                risk_free_rate, right=position.right,
            )
            if iv:
                position.iv = iv
                greeks = compute_greeks(
                    position.spot, position.strike, horizon, iv, position.mid,
                    risk_free_rate, right=position.right,
                )
                if greeks:
                    position.delta = greeks.delta
                    position.prob_itm = greeks.prob_itm

        multiplier = 100.0 * position.contracts
        if position.is_short:
            position.unrealised = (position.entry_credit - position.mid) * multiplier
            position.capital = (
                position.strike * multiplier
                if position.right == "P"
                else position.spot * multiplier
            )
        else:
            position.unrealised = (position.mid - position.entry_credit) * multiplier
            position.capital = position.entry_credit * multiplier

        position.pct_captured = (
            (position.entry_credit - position.mid) / position.entry_credit
            if position.entry_credit > 0
            else float("nan")
        )

    return positions


@dataclass
class SourceReport:
    """Which source supplied the positions, and what the others said.

    A fallback that happens silently is the failure this whole tool exists to
    prevent: the scanner once ran a full session on its built-in ticker list
    without saying so, and a hand-typed CSV carried three wrong entry prices
    for days. So the source is always reported, never assumed.
    """

    used: str | None = None
    attempts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        """True when positions came from a broker rather than a local file."""
        return self.used in {"ibkr", "alpaca"}


def load_positions(
    source: str,
    *,
    provider: AlpacaProvider | None = None,
    path: Path = DEFAULT_POSITIONS_FILE,
    host: str = "127.0.0.1",
    port: int = 7497,
) -> tuple[list[OpenOption], SourceReport]:
    """Load positions and report where they came from."""
    report = SourceReport()

    def _one(name: str) -> list[OpenOption]:
        if name == "csv":
            return read_positions_csv(path)
        if name == "alpaca":
            if provider is None:
                raise FetchError("Alpaca provider required for --source alpaca")
            return read_positions_alpaca(provider)
        if name == "ibkr":
            return read_positions_ibkr(host, port)
        raise ValueError(f"unknown source {name!r}")

    if source != "auto":
        found = _one(source)
        report.used = source
        report.attempts.append((source, f"{len(found)} position(s)"))
        return found, report

    # ib_async logs a connection failure at ERROR level before raising. In
    # auto mode an unreachable TWS is an expected fallthrough, not a fault,
    # and the noise would swamp a scheduled job's log.
    import logging

    ib_logger = logging.getLogger("ib_async")
    previous = ib_logger.level
    ib_logger.setLevel(logging.CRITICAL)
    try:
        for candidate in ("ibkr", "alpaca", "csv"):
            try:
                found = _one(candidate)
            except Exception as exc:
                report.attempts.append((candidate, _short_reason(exc)))
                continue
            if found:
                report.attempts.append((candidate, f"{len(found)} position(s)"))
                report.used = candidate
                return found, report
            report.attempts.append((candidate, "no positions"))
    finally:
        ib_logger.setLevel(previous)
    return [], report


def _short_reason(exc: Exception) -> str:
    """One readable line explaining why a source was unavailable."""
    text = str(exc).split("\n")[0]
    if "Connect call failed" in text or "Could not reach TWS" in text:
        return "TWS not reachable"
    return text[:70] if text else exc.__class__.__name__

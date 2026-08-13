"""Market-data providers.

Alpaca is the default because it is the only source that actually works from
this machine: the IBKR paper account carries no market-data entitlement (every
quote returns error 10089 and ``reqHistoricalData`` times out), so the original
IBKR-only scanner could never return a row.

The free Alpaca tier supplies underlying bars and option bid/ask/volume but no
greeks, implied volatility or open interest. Greeks and IV are computed in
``pricing``; open interest is simply unavailable and the strategy layer gates
liquidity on spread, quote size and volume instead of pretending otherwise.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol

from .netio import FetchError, get_json

ALPACA_DATA_URL = "https://data.alpaca.markets"
ALPACA_TRADE_URL = "https://paper-api.alpaca.markets"

# AAPL260805P00205000 -> root, expiry, right, strike in thousandths.
OCC_PATTERN = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<right>[PC])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    """A tradable option quote, normalised across providers."""

    occ_symbol: str
    underlying: str
    expiration: date
    strike: float
    right: str  # "P" or "C"
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    volume: float
    open_interest: int | None
    quote_time: datetime | None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid if self.mid > 0 else float("inf")


class Provider(Protocol):
    name: str

    def daily_bars(self, symbol: str, lookback_days: int) -> list[Bar]: ...

    def spot(self, symbol: str) -> tuple[float, datetime | None]: ...

    def option_chain(
        self,
        symbol: str,
        *,
        expiry_from: date,
        expiry_to: date,
        strike_min: float,
        strike_max: float,
        right: str,
    ) -> list[Quote]: ...


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file without clobbering real env vars."""
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def parse_occ(symbol: str) -> tuple[str, date, str, float] | None:
    match = OCC_PATTERN.match(symbol)
    if not match:
        return None
    ymd = match.group("ymd")
    try:
        expiration = datetime.strptime(ymd, "%y%m%d").date()
    except ValueError:
        return None
    return (
        match.group("root"),
        expiration,
        match.group("right"),
        int(match.group("strike")) / 1000.0,
    )


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if result == result else default  # filter NaN


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class AlpacaProvider:
    """Free-tier Alpaca.

    The free tier splits by recency, not by product: delayed *historical* SIP
    data is included, while real-time SIP quotes are not. So bars come from
    `sip` (full consolidated volume) and live snapshots from `iex`.

    The distinction matters for any volume threshold. IEX prints roughly 3% of
    consolidated volume, so screening on IEX bars silently applied a limit
    about thirty times tighter than intended, and ranked names by their IEX
    share rather than by real liquidity.
    """

    name = "alpaca"

    def __init__(
        self,
        key_id: str | None = None,
        secret_key: str | None = None,
        *,
        option_feed: str = "indicative",
        bar_feed: str = "sip",
        quote_feed: str = "iex",
    ) -> None:
        load_dotenv()
        self.key_id = key_id or os.environ.get("ALPACA_API_KEY_ID", "")
        self.secret_key = secret_key or os.environ.get("ALPACA_API_SECRET_KEY", "")
        if not self.key_id or not self.secret_key:
            raise FetchError(
                "Alpaca credentials missing. Set ALPACA_API_KEY_ID and "
                "ALPACA_API_SECRET_KEY in the environment or in a .env file "
                "next to this repository (see .env.example)."
            )
        self.option_feed = option_feed
        self.bar_feed = bar_feed
        self.quote_feed = quote_feed

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

    def daily_bars(self, symbol: str, lookback_days: int = 400) -> list[Bar]:
        start = (date.today() - timedelta(days=lookback_days)).isoformat()
        bars: list[Bar] = []
        page_token: str | None = None
        while True:
            payload = get_json(
                f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars",
                params={
                    "timeframe": "1Day",
                    "start": start,
                    "adjustment": "split",
                    "feed": self.bar_feed,
                    "limit": 10000,
                    "page_token": page_token,
                },
                headers=self._headers,
            )
            for row in payload.get("bars") or []:
                day = _parse_ts(row.get("t"))
                if day is None:
                    continue
                bars.append(
                    Bar(
                        day=day.date(),
                        open=_as_float(row.get("o")),
                        high=_as_float(row.get("h")),
                        low=_as_float(row.get("l")),
                        close=_as_float(row.get("c")),
                        volume=_as_float(row.get("v")),
                    )
                )
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return [b for b in bars if b.close > 0]

    def spot(self, symbol: str) -> tuple[float, datetime | None]:
        """Latest trade, falling back through quote mid then daily close.

        Pre-market the `latestTrade` can be an odd-lot print far from fair
        value, so a two-sided quote mid is preferred when both sides exist.
        """
        payload = get_json(
            f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/snapshot",
            params={"feed": self.quote_feed},
            headers=self._headers,
        )
        quote = payload.get("latestQuote") or {}
        bid, ask = _as_float(quote.get("bp")), _as_float(quote.get("ap"))
        if bid > 0 and ask > 0 and ask >= bid:
            return (bid + ask) / 2, _parse_ts(quote.get("t"))

        trade = payload.get("latestTrade") or {}
        price = _as_float(trade.get("p"))
        if price > 0:
            return price, _parse_ts(trade.get("t"))

        daily = payload.get("dailyBar") or payload.get("prevDailyBar") or {}
        return _as_float(daily.get("c")), _parse_ts(daily.get("t"))

    def option_chain(
        self,
        symbol: str,
        *,
        expiry_from: date,
        expiry_to: date,
        strike_min: float,
        strike_max: float,
        right: str,
    ) -> list[Quote]:
        """Server-side filtered chain request.

        Filtering by expiry, strike and right keeps a typical response under
        one page; an unfiltered snapshot call for a liquid name returns 1,000+
        contracts and forces pagination.
        """
        quotes: list[Quote] = []
        page_token: str | None = None
        while True:
            payload = get_json(
                f"{ALPACA_DATA_URL}/v1beta1/options/snapshots/{symbol}",
                params={
                    "feed": self.option_feed,
                    "type": "put" if right == "P" else "call",
                    "expiration_date_gte": expiry_from.isoformat(),
                    "expiration_date_lte": expiry_to.isoformat(),
                    "strike_price_gte": round(strike_min, 2),
                    "strike_price_lte": round(strike_max, 2),
                    "limit": 1000,
                    "page_token": page_token,
                },
                headers=self._headers,
            )
            for occ, snap in (payload.get("snapshots") or {}).items():
                parsed = parse_occ(occ)
                if parsed is None:
                    continue
                _, expiration, contract_right, strike = parsed
                quote = snap.get("latestQuote") or {}
                daily = snap.get("dailyBar") or {}
                quotes.append(
                    Quote(
                        occ_symbol=occ,
                        underlying=symbol,
                        expiration=expiration,
                        strike=strike,
                        right=contract_right,
                        bid=_as_float(quote.get("bp")),
                        ask=_as_float(quote.get("ap")),
                        bid_size=_as_float(quote.get("bs")),
                        ask_size=_as_float(quote.get("as")),
                        volume=_as_float(daily.get("v")),
                        open_interest=None,  # not on the free tier
                        quote_time=_parse_ts(quote.get("t")),
                    )
                )
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return sorted(quotes, key=lambda q: (q.expiration, q.strike))

    def positions(self) -> dict[str, tuple[float, float]]:
        """Equity holdings as ``{symbol: (shares, average_cost)}``.

        Used to find which covered calls you can actually write.
        """
        payload = get_json(
            f"{ALPACA_TRADE_URL}/v2/positions", headers=self._headers
        )
        result: dict[str, tuple[float, float]] = {}
        for row in payload or []:
            if row.get("asset_class") != "us_equity":
                continue
            symbol = str(row.get("symbol", "")).upper()
            shares = _as_float(row.get("qty"))
            if symbol and shares > 0:
                result[symbol] = (shares, _as_float(row.get("avg_entry_price")))
        return result

    def market_clock(self) -> dict:
        return get_json(f"{ALPACA_TRADE_URL}/v2/clock", headers=self._headers)


class IbkrProvider:
    """Optional IBKR path for when a market-data subscription exists.

    Kept behind ``--provider ibkr`` so the default run never depends on TWS.
    Raises immediately with a readable message if entitlements are missing.
    """

    name = "ibkr"

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 21) -> None:
        from ib_async import IB  # imported lazily: optional dependency

        self.ib = IB()
        self.ib.connect(host, port, clientId=client_id, readonly=True, timeout=15)
        self.ib.reqMarketDataType(3)

    def _qualified(self, symbol: str):
        from ib_async import Stock

        contracts = self.ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not contracts:
            raise FetchError(f"IBKR could not qualify {symbol}")
        return contracts[0]

    def daily_bars(self, symbol: str, lookback_days: int = 400) -> list[Bar]:
        stock = self._qualified(symbol)
        raw = self.ib.reqHistoricalData(
            stock,
            endDateTime="",
            durationStr=f"{min(lookback_days, 365)} D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not raw:
            raise FetchError(
                f"IBKR returned no history for {symbol}. This account most "
                "likely lacks a market-data subscription; use --provider alpaca."
            )
        return [
            Bar(
                day=b.date if isinstance(b.date, date) else datetime.strptime(str(b.date), "%Y%m%d").date(),
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
            for b in raw
            if float(b.close) > 0
        ]

    def spot(self, symbol: str) -> tuple[float, datetime | None]:
        stock = self._qualified(symbol)
        ticker = self.ib.reqMktData(stock, "", False, False)
        try:
            self.ib.sleep(2.5)
            for value in (ticker.marketPrice(), ticker.last, ticker.close):
                price = _as_float(value)
                if price > 0:
                    return price, datetime.now(timezone.utc)
        finally:
            self.ib.cancelMktData(stock)
        raise FetchError(f"IBKR returned no price for {symbol} (entitlement?)")

    def option_chain(
        self,
        symbol: str,
        *,
        expiry_from: date,
        expiry_to: date,
        strike_min: float,
        strike_max: float,
        right: str,
    ) -> list[Quote]:
        from ib_async import Option

        stock = self._qualified(symbol)
        params = self.ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        smart = [p for p in params if p.exchange == "SMART" and p.multiplier == "100"]
        chain = smart[0] if smart else (params[0] if params else None)
        if chain is None:
            return []

        expirations = [
            e
            for e in sorted(chain.expirations)
            if expiry_from <= datetime.strptime(e, "%Y%m%d").date() <= expiry_to
        ]
        strikes = [s for s in sorted(chain.strikes) if strike_min <= s <= strike_max]
        contracts = [
            Option(symbol, expiry, strike, right, "SMART", multiplier="100",
                   currency="USD", tradingClass=chain.tradingClass)
            for expiry in expirations
            for strike in strikes
        ]
        contracts = [c for c in self.ib.qualifyContracts(*contracts) if c.conId]
        if not contracts:
            return []

        tickers = [self.ib.reqMktData(c, "101", False, False) for c in contracts]
        try:
            self.ib.sleep(8)
        finally:
            for contract in contracts:
                self.ib.cancelMktData(contract)

        quotes = []
        for contract, ticker in zip(contracts, tickers):
            open_interest = ticker.putOpenInterest if right == "P" else ticker.callOpenInterest
            quotes.append(
                Quote(
                    occ_symbol=contract.localSymbol or "",
                    underlying=symbol,
                    expiration=datetime.strptime(
                        contract.lastTradeDateOrContractMonth[:8], "%Y%m%d"
                    ).date(),
                    strike=float(contract.strike),
                    right=right,
                    bid=_as_float(ticker.bid),
                    ask=_as_float(ticker.ask),
                    bid_size=_as_float(ticker.bidSize),
                    ask_size=_as_float(ticker.askSize),
                    volume=_as_float(ticker.volume),
                    open_interest=int(_as_float(open_interest)) or None,
                    quote_time=datetime.now(timezone.utc),
                )
            )
        return sorted(quotes, key=lambda q: (q.expiration, q.strike))

    def close(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()


def get_provider(name: str = "alpaca", **kwargs) -> Provider:
    if name == "alpaca":
        return AlpacaProvider(**kwargs)
    if name == "ibkr":
        return IbkrProvider(**kwargs)
    raise ValueError(f"unknown provider {name!r}")

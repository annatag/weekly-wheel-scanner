"""Build the scan universe from live broker data instead of a fixed list.

A hand-maintained ticker list goes stale in both directions: it keeps names
that have drifted out of the price band or lost their options listing, and it
never discovers new ones. This screens every optionable US equity Alpaca will
trade and keeps the ones that suit a wheel.

Screening runs in two stages because per-symbol requests do not scale to ~6,000
tickers. Stage one uses the multi-symbol bars endpoint, several hundred symbols
per call, to reject on price and liquidity. Stage two computes the finer
statistics only for what survived.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .netio import FetchError, get_json
from .providers import ALPACA_DATA_URL, ALPACA_TRADE_URL, AlpacaProvider

# Alpaca caps the multi-symbol endpoints well above this, but large batches
# make one slow symbol stall the whole request and complicate retries.
BATCH_SIZE = 250

CACHE_TTL_SECONDS = 20 * 3600

# Leveraged and inverse products track a daily-reset derivative rather than a
# business, so assignment hands you something you would never choose to hold.
# Word boundaries matter: "Ultragenyx" is a biotech, not a 2x ETF.
LEVERAGED_PATTERNS = re.compile(
    r"\b(?:[23](?:\.\d)?x|ultra(?:pro|short)?|inverse|bull|bear|leveraged|"
    r"daily\s+\w+\s+(?:bull|bear)|-1x|1\.5x)\b",
    re.IGNORECASE,
)

# Structured products and funds whose option chains are thin or whose risk is
# not equity-like.
EXCLUDE_NAME_PATTERNS = re.compile(
    r"\b(?:etn|note[s]?\s+due|trust\s+preferred|warrant[s]?|unit[s]?|"
    r"depositary|acquisition\s+corp|spac|royalty\s+trust)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UniverseFilters:
    """Screen settings. Prices are per share; volume is real consolidated."""

    min_price: float = 5.0
    # A single contract secures 100 shares, so the sleeve ceiling divided by
    # 100 is the highest strike that fits one contract.
    max_price: float = 150.0
    min_dollar_volume: float = 50_000_000
    min_history_days: int = 60
    # You cannot sell meaningful premium on something that does not move.
    # Cash-equivalent funds such as SGOV clear every liquidity bar while
    # realising well under 2% annualised, so screen on movement directly
    # rather than trying to name every such fund.
    min_realised_vol: float = 0.15
    max_symbols: int = 250
    exclude_leveraged: bool = True
    exclude_structured: bool = True
    allow_etfs: bool = True


@dataclass
class UniverseEntry:
    symbol: str
    name: str
    exchange: str
    price: float
    dollar_volume: float
    realised_vol: float
    bars: int


@dataclass
class ScreenReport:
    considered: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    kept: list[UniverseEntry] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def _chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start: start + size]


def _realised_vol(closes: list[float]) -> float:
    """Annualised close-to-close volatility, NaN when unmeasurable."""
    import math
    import statistics

    returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i] > 0 and closes[i - 1] > 0
    ]
    if len(returns) < 10:
        return float("nan")
    return statistics.pstdev(returns) * math.sqrt(252)


def fetch_optionable_assets(provider: AlpacaProvider) -> list[dict]:
    """Every active, tradable US equity that Alpaca flags as having options."""
    payload = get_json(
        f"{ALPACA_TRADE_URL}/v2/assets",
        params={"status": "active", "asset_class": "us_equity"},
        headers=provider._headers,
        timeout=90,
    )
    return [
        asset
        for asset in payload or []
        if asset.get("tradable")
        and "has_options" in (asset.get("attributes") or [])
    ]


def _symbol_is_plain(symbol: str) -> bool:
    """Reject share-class, warrant and unit tickers.

    Alpaca renders these with punctuation (BRK.B, ABC.WS, XYZ.U). Their option
    chains are absent or illiquid, and the punctuation breaks OCC symbols.
    """
    return symbol.isalpha() and 1 <= len(symbol) <= 5


def prefilter_assets(assets: list[dict], filters: UniverseFilters,
                     report: ScreenReport) -> list[dict]:
    """Name and symbol screening, before spending any market-data calls."""
    kept = []
    for asset in assets:
        symbol = str(asset.get("symbol", "")).upper()
        name = str(asset.get("name", ""))
        report.considered += 1

        if not _symbol_is_plain(symbol):
            report.reject("non-standard ticker (class/warrant/unit)")
            continue
        if filters.exclude_leveraged and LEVERAGED_PATTERNS.search(name):
            report.reject("leveraged or inverse product")
            continue
        if filters.exclude_structured and EXCLUDE_NAME_PATTERNS.search(name):
            report.reject("structured product or shell")
            continue
        kept.append(asset)
    return kept


def fetch_bulk_bars(
    provider: AlpacaProvider,
    symbols: list[str],
    *,
    lookback_days: int = 90,
    progress: bool = True,
) -> dict[str, list[dict]]:
    """Daily bars for many symbols using the multi-symbol endpoint.

    Uses the SIP feed: IEX carries roughly 3% of consolidated volume, so an
    IEX-based liquidity screen ranks names by their IEX share rather than by
    how much actually trades.
    """
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    results: dict[str, list[dict]] = {}
    batches = list(_chunks(symbols, BATCH_SIZE))

    for index, batch in enumerate(batches, 1):
        page_token = None
        while True:
            try:
                payload = get_json(
                    f"{ALPACA_DATA_URL}/v2/stocks/bars",
                    params={
                        "symbols": ",".join(batch),
                        "timeframe": "1Day",
                        "start": start,
                        "adjustment": "split",
                        "feed": provider.bar_feed,
                        "limit": 10000,
                        "page_token": page_token,
                    },
                    headers=provider._headers,
                    timeout=120,
                )
            except FetchError:
                # One bad batch should not lose the whole screen.
                break
            for symbol, rows in (payload.get("bars") or {}).items():
                results.setdefault(symbol, []).extend(rows)
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        if progress:
            # Carriage returns only overwrite on a terminal; when the output
            # is piped to a file they concatenate into one unreadable line.
            import sys

            if sys.stdout.isatty():
                print(f"\r  fetching bars: batch {index}/{len(batches)} "
                      f"({len(results)} symbols)", end="", flush=True)
            elif index == len(batches):
                print(f"  fetched bars for {len(results)} symbols "
                      f"in {len(batches)} batches")
        time.sleep(0.05)
    if progress and __import__("sys").stdout.isatty():
        print()
    return results


def screen(
    provider: AlpacaProvider,
    filters: UniverseFilters | None = None,
    *,
    progress: bool = True,
) -> ScreenReport:
    """Run the full screen and return the ranked survivors plus diagnostics."""
    filters = filters or UniverseFilters()
    report = ScreenReport()

    if progress:
        print("Fetching optionable assets from Alpaca...")
    assets = fetch_optionable_assets(provider)
    if progress:
        print(f"  {len(assets)} optionable, tradable US equities")

    candidates = prefilter_assets(assets, filters, report)
    names = {a["symbol"]: a for a in candidates}
    if progress:
        print(f"  {len(candidates)} remain after name and ticker screening")

    bars = fetch_bulk_bars(
        provider, sorted(names), lookback_days=120, progress=progress
    )

    for symbol, asset in names.items():
        rows = bars.get(symbol) or []
        if len(rows) < filters.min_history_days:
            report.reject("insufficient trading history")
            continue

        recent = rows[-20:]
        price = float(recent[-1].get("c") or 0)
        if price <= 0:
            report.reject("no valid price")
            continue
        if not filters.min_price <= price <= filters.max_price:
            report.reject("price outside band")
            continue

        dollar_volume = sum(
            float(r.get("c") or 0) * float(r.get("v") or 0) for r in recent
        ) / len(recent)
        if dollar_volume < filters.min_dollar_volume:
            report.reject("dollar volume below minimum")
            continue

        realised = _realised_vol([float(r.get("c") or 0) for r in rows[-40:]])
        if realised != realised:
            report.reject("could not measure volatility")
            continue
        if realised < filters.min_realised_vol:
            report.reject("too little volatility to sell premium")
            continue

        report.kept.append(
            UniverseEntry(
                symbol=symbol,
                name=str(asset.get("name", "")),
                exchange=str(asset.get("exchange", "")),
                price=price,
                dollar_volume=dollar_volume,
                realised_vol=realised,
                bars=len(rows),
            )
        )

    report.kept.sort(key=lambda e: e.dollar_volume, reverse=True)
    if filters.max_symbols > 0:
        report.kept = report.kept[: filters.max_symbols]
    return report


def write_symbols_file(path: Path, report: ScreenReport,
                       filters: UniverseFilters) -> None:
    """Write symbols.txt with the screen recorded in comments."""
    lines = [
        "# Generated by build_universe.py - do not hand-edit.",
        f"# Built {date.today().isoformat()} from Alpaca optionable equities.",
        f"# Filters: price ${filters.min_price:g}-${filters.max_price:g}, "
        f"min ${filters.min_dollar_volume / 1e6:g}M/day consolidated volume, "
        f"top {filters.max_symbols} by liquidity.",
        f"# {len(report.kept)} symbols. Remove any you would not want to own.",
        "",
    ]
    lines.extend(entry.symbol for entry in report.kept)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_cached(path: Path, ttl: int = CACHE_TTL_SECONDS) -> list[str] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if time.time() - payload.get("built_at", 0) > ttl:
        return None
    return payload.get("symbols") or None


def save_cache(path: Path, report: ScreenReport) -> None:
    try:
        path.write_text(
            json.dumps(
                {
                    "built_at": time.time(),
                    "symbols": [e.symbol for e in report.kept],
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

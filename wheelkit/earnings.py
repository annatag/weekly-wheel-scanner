"""Earnings-date lookup.

Selling a 7-21 day option through an earnings report is the single fastest way
to turn a high-probability trade into a loss: implied volatility collapses but
the gap risk is realised in one session. The old scanner gated on a manually
maintained ``earnings.csv`` that shipped empty, so the exclusion silently never
fired. This fetches the Nasdaq calendar and treats the CSV as an override.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import date, timedelta
from pathlib import Path

from .netio import FetchError, get_json

NASDAQ_CALENDAR = "https://api.nasdaq.com/api/calendar/earnings"
CACHE_TTL_SECONDS = 12 * 3600


def load_overrides(path: Path) -> dict[str, date]:
    """Manual symbol,earnings_date pairs. These always win over the feed."""
    result: dict[str, date] = {}
    if not path.exists():
        return result
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = (row.get("symbol") or "").strip().upper()
            raw = (row.get("earnings_date") or "").strip()
            if not symbol or not raw or symbol.startswith("#"):
                continue
            try:
                result[symbol] = date.fromisoformat(raw)
            except ValueError:
                continue
    return result


def _fetch_day(day: date) -> dict[str, date]:
    payload = get_json(
        NASDAQ_CALENDAR,
        params={"date": day.isoformat()},
        headers={"Referer": "https://www.nasdaq.com/"},
        retries=2,
    )
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    return {
        str(row.get("symbol", "")).strip().upper(): day
        for row in rows
        if row.get("symbol")
    }


DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / ".earnings_cache.json"


def read_cache(
    cache_path: Path | None = None, *, horizon_days: int = 0, max_age: float | None = None
) -> tuple[dict[str, date], float] | None:
    """The calendar last written to disk, with its age in seconds.

    Age is returned rather than judged, because how stale is too stale depends
    on the caller. A scan wants today's calendar; an offline run wants the
    best answer that exists, which is a week-old calendar rather than none.
    """
    cache_path = cache_path or DEFAULT_CACHE_PATH
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        dates = {
            symbol: date.fromisoformat(value)
            for symbol, value in (cached.get("dates") or {}).items()
        }
    except (ValueError, OSError, TypeError):
        return None
    if not dates or cached.get("horizon_days", 0) < horizon_days:
        return None
    age = max(time.time() - cached.get("fetched_at", 0), 0.0)
    if max_age is not None and age > max_age:
        return None
    return dates, age


def fetch_calendar(
    horizon_days: int = 45, cache_path: Path | None = None
) -> dict[str, date]:
    """Map every symbol reporting in the next ``horizon_days`` to its date.

    One request per calendar day is slow, so results are cached on disk for
    half a day. A partial calendar is still returned if some days fail.
    """
    cache_path = cache_path or DEFAULT_CACHE_PATH
    today = date.today()

    fresh = read_cache(
        cache_path, horizon_days=horizon_days, max_age=CACHE_TTL_SECONDS
    )
    if fresh is not None:
        return fresh[0]

    calendar: dict[str, date] = {}
    failures = 0
    for offset in range(horizon_days):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            for symbol, value in _fetch_day(day).items():
                calendar.setdefault(symbol, value)
        except FetchError:
            failures += 1
            if failures > 5:
                break

    if calendar:
        try:
            cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": time.time(),
                        "horizon_days": horizon_days,
                        "dates": {s: d.isoformat() for s, d in calendar.items()},
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return calendar


class EarningsCalendar:
    """Combined view of the fetched calendar plus manual overrides."""

    def __init__(
        self, dates: dict[str, date], available: bool, source: str = "feed"
    ) -> None:
        self._dates = dates
        self.available = available
        # How the dates were obtained, for the line the report prints. An
        # exclusion that quietly stopped applying is the failure this whole
        # module exists to avoid, so the source is always stated.
        self.source = source

    @classmethod
    def build(
        cls,
        override_path: Path,
        *,
        horizon_days: int = 45,
        offline: bool = False,
        cache_path: Path | None = None,
    ) -> "EarningsCalendar":
        """Feed first, then the cache at any age, then the overrides alone.

        The cache used to be reachable only through the feed's freshness
        check, so ``--offline-earnings`` and a failed fetch both produced an
        empty calendar - and an empty calendar excludes nothing. The gate was
        then off with no way to tell from the output, which is exactly the
        silent failure this replaced. A week-old calendar is a far better
        answer than none: earnings dates are published weeks ahead and rarely
        move by more than a day.
        """
        fetched: dict[str, date] = {}
        available = False
        source = "none"

        if not offline:
            try:
                fetched = fetch_calendar(horizon_days, cache_path)
                available = bool(fetched)
                source = "feed" if available else "none"
            except FetchError:
                available = False

        if not available:
            cached = read_cache(cache_path, horizon_days=horizon_days)
            if cached is not None:
                fetched, age = cached
                available = True
                days = age / 86400.0
                source = (
                    "cache (fetched today)" if days < 1
                    else f"cache ({days:.0f} days old)"
                )

        overrides = load_overrides(override_path)
        fetched.update(overrides)
        if overrides and not available:
            source = f"earnings.csv only ({len(overrides)} override(s))"
        return cls(fetched, available, source)

    def next_date(self, symbol: str) -> date | None:
        return self._dates.get(symbol.upper())

    def reports_between(self, symbol: str, start: date, end: date) -> bool:
        found = self.next_date(symbol)
        return found is not None and start <= found <= end

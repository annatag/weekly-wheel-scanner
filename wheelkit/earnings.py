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


def fetch_calendar(
    horizon_days: int = 45, cache_path: Path | None = None
) -> dict[str, date]:
    """Map every symbol reporting in the next ``horizon_days`` to its date.

    One request per calendar day is slow, so results are cached on disk for
    half a day. A partial calendar is still returned if some days fail.
    """
    cache_path = cache_path or Path(__file__).resolve().parent.parent / ".earnings_cache.json"
    today = date.today()

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            fresh = time.time() - cached.get("fetched_at", 0) < CACHE_TTL_SECONDS
            if fresh and cached.get("horizon_days", 0) >= horizon_days:
                return {
                    symbol: date.fromisoformat(value)
                    for symbol, value in cached.get("dates", {}).items()
                }
        except (ValueError, OSError):
            pass

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

    def __init__(self, dates: dict[str, date], available: bool) -> None:
        self._dates = dates
        self.available = available

    @classmethod
    def build(
        cls,
        override_path: Path,
        *,
        horizon_days: int = 45,
        offline: bool = False,
    ) -> "EarningsCalendar":
        fetched: dict[str, date] = {}
        available = False
        if not offline:
            try:
                fetched = fetch_calendar(horizon_days)
                available = bool(fetched)
            except FetchError:
                available = False
        fetched.update(load_overrides(override_path))
        return cls(fetched, available)

    def next_date(self, symbol: str) -> date | None:
        return self._dates.get(symbol.upper())

    def reports_between(self, symbol: str, start: date, end: date) -> bool:
        found = self.next_date(symbol)
        return found is not None and start <= found <= end

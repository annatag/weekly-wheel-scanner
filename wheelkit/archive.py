"""Keep every scan, so the screen can eventually be judged.

Nothing in this toolkit could previously answer "does the score predict
anything". `wheel_scan_results.csv` is overwritten by the next run, so the
only surviving record was the handful of contracts actually traded - one or
two a week, against seven scoring terms and dozens of hand-chosen thresholds.
At that rate a real improvement in win rate would take years to become
visible, and a bad change would never become visible at all.

Every ranked candidate is a free observation. The scan already produces ten
per run and throws nine of them away. Archiving them costs a file write, and
the outcome of a short put is knowable from one number - where the underlying
closed on the expiry - which the bar history already carries.

This module stores; `wheel_backtest.py` resolves and reports. Neither ever
changes what the live scan recommends.
"""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

DEFAULT_ARCHIVE_DIR = Path("archive")
SCAN_DIR = "scans"
ATM_IV_FILE = "atm_iv.csv"
OUTCOME_FILE = "outcomes.csv"

# Only the fields an evaluation needs. The full candidate has fifty-odd
# columns; carrying all of them into a file that grows every weekday makes it
# slow to read and no more informative.
ARCHIVED_FIELDS = (
    "scan_id", "scanned_on", "symbol", "right", "occ_symbol", "expiration",
    "dte", "strike", "spot", "mid", "bid", "ask", "spread_pct",
    "option_volume", "iv", "delta", "prob_itm", "prob_profit", "vrp",
    "skew_ratio", "term_slope", "rv20", "rv_percentile", "max_drawdown_60d",
    "gap_down_p05", "cushion_sigmas", "cushion_pct", "breakeven",
    "return_on_capital", "annualised_return", "trend_score", "setup",
    "contracts", "capital", "credit", "score",
)

OUTCOME_FIELDS = (
    "scan_id", "scanned_on", "symbol", "right", "expiration", "strike",
    "delta", "score", "credit", "capital", "breakeven",
    "close_at_expiry", "expired_worthless", "assigned",
    "pnl_per_share", "pnl", "resolved_on",
)


def scan_id(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime("%Y-%m-%d-%H%M")


def _ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def archive_scan(
    candidates: list,
    *,
    directory: Path = DEFAULT_ARCHIVE_DIR,
    when: datetime | None = None,
) -> Path | None:
    """Write every ranked candidate to a dated file. Returns the path.

    Deliberately writes all of them, not the top N. The candidates that ranked
    fourth through tenth are the control group: without them you can only ever
    measure how the trades you took performed, never whether the ranking put
    the right ones on top.
    """
    if not candidates:
        return None
    when = when or datetime.now()
    identifier = scan_id(when)
    target = _ensure(directory / SCAN_DIR) / f"{identifier}.csv"

    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ARCHIVED_FIELDS))
        writer.writeheader()
        for candidate in candidates:
            row = asdict(candidate)
            row["scan_id"] = identifier
            row["scanned_on"] = when.date().isoformat()
            writer.writerow({k: row.get(k, "") for k in ARCHIVED_FIELDS})
    return target


def log_atm_iv(
    readings: dict[str, float],
    *,
    directory: Path = DEFAULT_ARCHIVE_DIR,
    when: date | None = None,
) -> Path | None:
    """Append one at-the-money implied volatility per symbol per day.

    This is the seed for IV rank, which cannot be computed at all today: the
    free data tier carries no option history, so the only route to "is this
    option expensive for this name" is to start writing it down. The value
    arrives in about three months and is zero before then, which is exactly
    why it has to start now rather than when it is wanted.
    """
    if not readings:
        return None
    when = when or date.today()
    target = _ensure(directory) / ATM_IV_FILE
    exists = target.exists()

    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["observed_on", "symbol", "atm_iv"])
        for symbol, iv in sorted(readings.items()):
            if iv == iv and iv > 0:
                writer.writerow([when.isoformat(), symbol.upper(), f"{iv:.6f}"])
    return target


def read_atm_iv_history(
    directory: Path = DEFAULT_ARCHIVE_DIR,
) -> dict[str, list[tuple[date, float]]]:
    """Everything logged so far, per symbol, oldest first."""
    target = directory / ATM_IV_FILE
    if not target.exists():
        return {}
    history: dict[str, list[tuple[date, float]]] = {}
    with target.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                observed = date.fromisoformat(row["observed_on"])
                value = float(row["atm_iv"])
            except (KeyError, ValueError):
                continue
            history.setdefault(row["symbol"].upper(), []).append((observed, value))
    for entries in history.values():
        entries.sort()
    return history


def iv_rank(symbol: str, current: float, history: dict, minimum: int = 60) -> float:
    """Where today's IV sits in this name's own logged range, 0-100.

    Returns NaN until there are enough observations. A rank computed from a
    fortnight of history is not a rank, and reporting one would be worse than
    reporting nothing.
    """
    entries = history.get(symbol.upper(), [])
    if len(entries) < minimum or current != current:
        return float("nan")
    values = [v for _, v in entries]
    low, high = min(values), max(values)
    if high - low <= 1e-9:
        return float("nan")
    return max(0.0, min(100.0, 100.0 * (current - low) / (high - low)))


def read_archived_scans(directory: Path = DEFAULT_ARCHIVE_DIR) -> list[dict]:
    """Every archived candidate across every run, oldest file first."""
    folder = directory / SCAN_DIR
    if not folder.exists():
        return []
    rows: list[dict] = []
    for path in sorted(folder.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def write_outcomes(
    outcomes: list[dict], *, directory: Path = DEFAULT_ARCHIVE_DIR
) -> Path:
    target = _ensure(directory) / OUTCOME_FILE
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTCOME_FIELDS))
        writer.writeheader()
        for row in outcomes:
            writer.writerow({k: row.get(k, "") for k in OUTCOME_FIELDS})
    return target


def read_outcomes(directory: Path = DEFAULT_ARCHIVE_DIR) -> list[dict]:
    target = directory / OUTCOME_FILE
    if not target.exists():
        return []
    with target.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))

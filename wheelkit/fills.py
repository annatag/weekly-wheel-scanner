"""What was actually sold, against what the scan actually suggested.

The scanner is graded on its recommendations, but a recommendation is not
what ends up in the account. A strike gets nudged a dollar for a better bid,
an expiry slides a week to clear an earnings date, a limit gets walked past
the floor to get filled at all. Every one of those is a reasonable decision at
the ticket, and every one of them breaks the link between "the model said
this" and "this is what happened". Grading a run against trades that drifted
from it measures nothing.

So a fill is recorded against the suggestion it came from, with the drift
computed at the moment of entry, while the scan that produced it is still on
disk. The suggested numbers are copied into the row rather than referenced,
because the scan file is overwritten by the next run and a pointer into a
deleted file is worse than no pointer.

Nothing here places or reads orders. It is a notebook you have to write in.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path

DEFAULT_FILLS_FILE = Path("fills.csv")

# Anything inside these is the same trade the scan described. Outside them the
# fill is a different trade that happens to share a ticker, and it is labelled
# so the grading can exclude it rather than quietly average it in.
STRIKE_TOLERANCE_PCT = 0.02
EXPIRY_TOLERANCE_DAYS = 3
CREDIT_TOLERANCE_PCT = 0.15


@dataclass
class Fill:
    """One entry, plus its exit once it has one."""

    recorded_at: date
    symbol: str
    right: str
    expiration: date
    strike: float
    contracts: int
    fill_credit: float  # per share, positive

    # The suggestion this came from. All optional: a trade entered without a
    # scan behind it is still worth logging, it just cannot be graded.
    scan_file: str = ""
    scan_date: date | None = None
    suggested_expiration: date | None = None
    suggested_strike: float = float("nan")
    suggested_mid: float = float("nan")
    suggested_limit_likely: float = float("nan")
    suggested_delta: float = float("nan")
    suggested_dte: float = float("nan")
    suggested_score: float = float("nan")

    drift: str = ""
    note: str = ""

    # Filled in when the position closes.
    closed_on: date | None = None
    close_debit: float = float("nan")  # per share paid to buy it back
    outcome: str = ""  # closed | expired | assigned | rolled

    @property
    def matches_suggestion(self) -> bool:
        return self.scan_file != "" and self.drift == ""

    @property
    def credit_received(self) -> float:
        return self.fill_credit * 100.0 * self.contracts

    @property
    def realised(self) -> float:
        """Dollars kept, once closed. NaN while the position is open."""
        if not self.outcome:
            return float("nan")
        debit = 0.0 if self.close_debit != self.close_debit else self.close_debit
        return (self.fill_credit - debit) * 100.0 * self.contracts

    @property
    def captured_pct(self) -> float:
        if not self.outcome or self.fill_credit <= 0:
            return float("nan")
        debit = 0.0 if self.close_debit != self.close_debit else self.close_debit
        return (self.fill_credit - debit) / self.fill_credit


def compute_drift(fill: Fill) -> str:
    """Plain description of how the fill differs from the suggestion.

    Empty means the fill is the suggested trade. Each clause names the number
    that moved, because "drifted" on its own tells you nothing about whether
    the difference mattered.
    """
    if not fill.scan_file:
        return ""
    parts: list[str] = []

    suggested_strike = fill.suggested_strike
    if suggested_strike == suggested_strike and suggested_strike > 0:
        gap = abs(fill.strike - suggested_strike) / suggested_strike
        if gap > STRIKE_TOLERANCE_PCT:
            parts.append(
                f"strike ${fill.strike:g} vs ${suggested_strike:g} suggested"
            )

    if fill.suggested_expiration is not None:
        days = (fill.expiration - fill.suggested_expiration).days
        if abs(days) > EXPIRY_TOLERANCE_DAYS:
            parts.append(
                f"expiry {fill.expiration:%b %d} vs "
                f"{fill.suggested_expiration:%b %d} suggested ({days:+d}d)"
            )

    reference = fill.suggested_limit_likely
    if reference != reference:
        reference = fill.suggested_mid
    if reference == reference and reference > 0:
        gap = (fill.fill_credit - reference) / reference
        if abs(gap) > CREDIT_TOLERANCE_PCT:
            direction = "above" if gap > 0 else "below"
            parts.append(
                f"credit ${fill.fill_credit:.2f} is {abs(gap):.0%} {direction} "
                f"the ${reference:.2f} the ladder expected"
            )

    return "; ".join(parts)


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------

FIELDNAMES = [f.name for f in fields(Fill)]


def _to_row(fill: Fill) -> dict[str, str]:
    row: dict[str, str] = {}
    for key, value in asdict(fill).items():
        if value is None:
            row[key] = ""
        elif isinstance(value, date):
            row[key] = value.isoformat()
        elif isinstance(value, float):
            row[key] = "" if value != value else f"{value:g}"
        else:
            row[key] = str(value)
    return row


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _from_row(row: dict[str, str]) -> Fill | None:
    expiration = _date(row.get("expiration", ""))
    recorded = _date(row.get("recorded_at", "")) or date.today()
    if not row.get("symbol") or expiration is None:
        return None
    return Fill(
        recorded_at=recorded,
        symbol=row["symbol"].strip().upper(),
        right=(row.get("right") or "P").strip().upper()[:1],
        expiration=expiration,
        strike=_float(row.get("strike", "")),
        contracts=int(_float(row.get("contracts", "1")) or 1),
        fill_credit=_float(row.get("fill_credit", "")),
        scan_file=(row.get("scan_file") or "").strip(),
        scan_date=_date(row.get("scan_date", "")),
        suggested_expiration=_date(row.get("suggested_expiration", "")),
        suggested_strike=_float(row.get("suggested_strike", "")),
        suggested_mid=_float(row.get("suggested_mid", "")),
        suggested_limit_likely=_float(row.get("suggested_limit_likely", "")),
        suggested_delta=_float(row.get("suggested_delta", "")),
        suggested_dte=_float(row.get("suggested_dte", "")),
        suggested_score=_float(row.get("suggested_score", "")),
        drift=(row.get("drift") or "").strip(),
        note=(row.get("note") or "").strip(),
        closed_on=_date(row.get("closed_on", "")),
        close_debit=_float(row.get("close_debit", "")),
        outcome=(row.get("outcome") or "").strip(),
    )


def load_fills(path: Path = DEFAULT_FILLS_FILE) -> list[Fill]:
    if not path.exists():
        return []
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return []
    out = []
    for row in csv.DictReader(lines):
        parsed = _from_row(row)
        if parsed is not None:
            out.append(parsed)
    return out


def save_fills(fills: list[Fill], path: Path = DEFAULT_FILLS_FILE) -> None:
    """Rewrite the whole file. Small enough that atomicity beats appending."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for fill in fills:
            writer.writerow(_to_row(fill))
    tmp.replace(path)


def append_fill(fill: Fill, path: Path = DEFAULT_FILLS_FILE) -> None:
    save_fills(load_fills(path) + [fill], path)


def find_open(
    fills: list[Fill], symbol: str, expiration: date, strike: float, right: str
) -> Fill | None:
    """The open fill matching a contract, most recent first."""
    for fill in reversed(fills):
        if (
            fill.symbol == symbol.upper()
            and fill.right == right.upper()[:1]
            and fill.expiration == expiration
            and abs(fill.strike - strike) < 1e-6
            and not fill.outcome
        ):
            return fill
    return None


# ---------------------------------------------------------------------
# Reading a suggestion back out of a scan
# ---------------------------------------------------------------------


@dataclass
class Suggestion:
    symbol: str
    right: str
    expiration: date | None
    strike: float
    mid: float
    limit_likely: float
    delta: float
    dte: float
    score: float


def read_suggestions(path: Path) -> list[Suggestion]:
    """Parse a scan results CSV into just the fields a fill is graded on."""
    if not path.exists():
        return []
    out: list[Suggestion] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            out.append(Suggestion(
                symbol=symbol,
                right=(row.get("right") or "P").strip().upper()[:1],
                expiration=_date(row.get("expiration", "")),
                strike=_float(row.get("strike", "")),
                mid=_float(row.get("mid", "")),
                limit_likely=_float(row.get("limit_likely", "")),
                delta=_float(row.get("delta", "")),
                dte=_float(row.get("dte", "")),
                score=_float(row.get("score", "")),
            ))
    return out


def best_match(
    suggestions: list[Suggestion], symbol: str, right: str, strike: float
) -> Suggestion | None:
    """The suggested contract a fill most plausibly came from.

    Matching on ticker and right, then on the nearest strike: a scan lists at
    most a couple of contracts per name, and the strike is what a ticket
    actually moves.
    """
    candidates = [
        s for s in suggestions
        if s.symbol == symbol.upper() and s.right == right.upper()[:1]
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(s.strike - strike))


def entry_date_index(
    path: Path = DEFAULT_FILLS_FILE,
) -> dict[tuple[str, str, date, int], date]:
    """When each logged contract was entered, keyed by the contract itself.

    The checkpoint wants the trade's original length, and a broker position
    does not carry one: IBKR reports what you hold, not when you opened it,
    and its execution history only reaches back to the current session. The
    fill log is the only place that number survives, which is a second reason
    to keep it - it started as a way to grade the scanner and turns out to be
    what makes the exit rule correct.
    """
    index: dict[tuple[str, str, date, int], date] = {}
    for fill in load_fills(path):
        key = (fill.symbol, fill.right, fill.expiration,
               int(round(fill.strike * 1000)))
        # Keep the earliest entry: a contract re-entered after a close is
        # still, for the checkpoint's purposes, a position that began then.
        if key not in index or fill.recorded_at < index[key]:
            index[key] = fill.recorded_at
    return index


def entry_date_for(
    index: dict[tuple[str, str, date, int], date],
    symbol: str,
    right: str,
    expiration: date,
    strike: float,
) -> date | None:
    return index.get(
        (symbol.upper(), right.upper()[:1], expiration,
         int(round(strike * 1000)))
    )


def scan_date_of(path: Path) -> date | None:
    """When a scan file was written. Used when no date is given explicitly."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None

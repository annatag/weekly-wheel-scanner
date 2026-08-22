"""Risk limits: what may be opened, how large, and when to act.

The scanner decides what is *worth* selling. Nothing enforced what actually
got sold, and the gap between the two is where the losses came from. On a
five-position paper book, four entries sat outside the scanner's own delta
band and the two worst were the two furthest outside it; the single position
inside the band was the clean winner.

So the rules live here, separately from the ranking, and apply at three
points:

* before opening, as a pass/fail gate on a specific contract;
* at sizing, so one cheap high-volatility name cannot absorb the whole sleeve;
* while open, as alerts when a position drifts out of its original thesis.

Nothing here places or closes an order. It reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

# Severity ordering used for sorting and exit codes.
INFO, WARN, URGENT = "INFO", "WARN", "URGENT"
_SEVERITY = {INFO: 0, WARN: 1, URGENT: 2}


@dataclass(frozen=True)
class RiskLimits:
    """Every threshold in one place, so a live account can run tighter."""

    # --- entry gate ---------------------------------------------------
    max_entry_delta: float = 0.22
    min_entry_delta: float = 0.10
    min_dte: int = 5
    max_dte: int = 45
    min_vrp: float = 1.0
    min_credit_per_share: float = 0.15
    max_spread_pct: float = 0.12
    require_otm: bool = True
    block_earnings_before_expiry: bool = True
    blocked_setups: tuple[str, ...] = ("falling knife",)

    # --- sizing -------------------------------------------------------
    account_value: float = 100_000.0
    # Most that one adverse two-sigma move may cost, as a share of account.
    risk_budget_pct: float = 0.02
    max_contracts_per_position: int = 10
    max_capital_per_position_pct: float = 0.15

    # --- portfolio ----------------------------------------------------
    max_open_positions: int = 8
    max_total_capital_pct: float = 0.60
    max_positions_per_symbol: int = 1
    max_positions_per_sector: int = 3

    # --- while open ---------------------------------------------------
    alert_delta: float = 0.50
    # Assignment odds that make a position urgent regardless of how much time
    # is left. Depth used to be invisible to the escalation: a contract 8% in
    # the money at 0.92 delta was only a warning because it had seven days,
    # while a shallower one on expiry day was urgent.
    urgent_assign_prob: float = 0.85
    profit_target_pct: float = 0.50
    gamma_window_dte: int = 3
    underlying_move_alert: float = 0.05
    roll_dte: int = 21


@dataclass
class Finding:
    level: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.message}"


def _sort(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: -_SEVERITY.get(f.level, 0))


def worst_level(findings: list[Finding]) -> str | None:
    return max(
        (f.level for f in findings), key=lambda l: _SEVERITY.get(l, 0), default=None
    )


# ---------------------------------------------------------------------
# Entry gate
# ---------------------------------------------------------------------


def check_entry(
    *,
    symbol: str,
    right: str,
    strike: float,
    spot: float,
    delta: float,
    dte: int,
    credit_per_share: float,
    spread_pct: float,
    vrp: float = float("nan"),
    setup: str = "unknown",
    earnings_date: date | None = None,
    expiration: date | None = None,
    limits: RiskLimits | None = None,
) -> list[Finding]:
    """Pass/fail a specific contract before it is sold.

    This is the check that was missing. A 0.53-delta put sold in the money
    is not a marginal call the ranking should weigh against premium; it is a
    different trade from the one the strategy describes, and it is refused.
    """
    limits = limits or RiskLimits()
    out: list[Finding] = []
    abs_delta = abs(delta)

    if abs_delta > limits.max_entry_delta:
        out.append(Finding(
            URGENT, "delta_too_high",
            f"delta {abs_delta:.2f} exceeds the {limits.max_entry_delta:.2f} "
            f"limit - roughly a {abs_delta:.0%} chance of assignment at entry",
        ))
    elif abs_delta < limits.min_entry_delta:
        out.append(Finding(
            WARN, "delta_too_low",
            f"delta {abs_delta:.2f} is below {limits.min_entry_delta:.2f}; "
            "the premium is unlikely to justify the capital",
        ))

    if limits.require_otm:
        itm = (right == "P" and strike >= spot) or (right == "C" and strike <= spot)
        if itm:
            out.append(Finding(
                URGENT, "sold_itm",
                f"strike ${strike:g} is in the money against a ${spot:,.2f} "
                "spot - this is assignment by design, not a premium sale",
            ))

    if dte < limits.min_dte:
        out.append(Finding(
            WARN, "too_close_to_expiry",
            f"{dte} DTE is inside the {limits.min_dte}-day floor, where gamma "
            "moves delta faster than you can react",
        ))
    elif dte > limits.max_dte:
        out.append(Finding(
            INFO, "long_dated",
            f"{dte} DTE is beyond the {limits.max_dte}-day ceiling; capital is "
            "committed for longer at a lower daily decay rate",
        ))

    if credit_per_share < limits.min_credit_per_share:
        out.append(Finding(
            WARN, "credit_too_small",
            f"${credit_per_share:.2f} per share is below the "
            f"${limits.min_credit_per_share:.2f} floor",
        ))

    if spread_pct > limits.max_spread_pct:
        out.append(Finding(
            WARN, "spread_too_wide",
            f"spread is {spread_pct:.1%}, above the {limits.max_spread_pct:.1%} "
            "limit - you lose the edge on the fill",
        ))

    if vrp == vrp and vrp < limits.min_vrp:
        out.append(Finding(
            WARN, "selling_cheap_vol",
            f"implied/realised is {vrp:.2f}; you are being paid less than the "
            "stock has actually been moving",
        ))

    if setup in limits.blocked_setups:
        out.append(Finding(
            URGENT, "blocked_setup",
            f"setup is '{setup}' - down on both the quarter and the month",
        ))

    if (
        limits.block_earnings_before_expiry
        and earnings_date is not None
        and expiration is not None
        and date.today() <= earnings_date <= expiration
    ):
        out.append(Finding(
            URGENT, "earnings_before_expiry",
            f"{symbol} reports {earnings_date:%b %d}, before the "
            f"{expiration:%b %d} expiry",
        ))

    return _sort(out)


# ---------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------


@dataclass
class SizingResult:
    contracts: int
    capital: float
    stress_move_pct: float
    stress_loss: float
    binding_constraint: str


def size_position(
    *,
    strike: float,
    spot: float,
    iv: float,
    dte: int,
    limits: RiskLimits | None = None,
) -> SizingResult | None:
    """Contracts sized so a two-sigma adverse move stays inside the budget.

    Filling a fixed cash sleeve puts the most contracts on the cheapest stock,
    which is usually the most volatile one. A $28 name took five contracts and
    a 10% week cost five times what one contract would have. Scaling by
    volatility inverts that: the more a name moves, the fewer you sell.
    """
    limits = limits or RiskLimits()
    if strike <= 0 or spot <= 0:
        return None

    per_contract = strike * 100.0
    budget = limits.account_value * limits.risk_budget_pct

    # Two standard deviations of the underlying over the holding period.
    horizon = max(dte, 1) / 365.0
    sigma = iv if iv == iv and iv > 0 else 0.35
    stress_move = 2.0 * sigma * math.sqrt(horizon)
    # Loss if the underlying falls by the stress move and lands below strike.
    stress_price = spot * (1 - stress_move)
    loss_per_contract = max(strike - stress_price, 0.0) * 100.0

    by_risk = (
        int(budget // loss_per_contract) if loss_per_contract > 0 else 10**6
    )
    by_capital = int(
        (limits.account_value * limits.max_capital_per_position_pct) // per_contract
    )
    by_cap = limits.max_contracts_per_position

    contracts = min(by_risk, by_capital, by_cap)
    binding = min(
        (("two-sigma risk budget", by_risk), ("capital per position", by_capital),
         ("contract cap", by_cap)),
        key=lambda kv: kv[1],
    )[0]

    if contracts < 1:
        return None
    return SizingResult(
        contracts=contracts,
        capital=contracts * per_contract,
        stress_move_pct=stress_move,
        stress_loss=contracts * loss_per_contract,
        binding_constraint=binding,
    )


# ---------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------


def check_portfolio(
    open_positions: list[dict],
    *,
    proposed: dict | None = None,
    limits: RiskLimits | None = None,
) -> list[Finding]:
    """Aggregate exposure across everything open, plus an optional new trade.

    Each position can pass its own gate and still be wrong collectively: five
    single-name puts sized to the same sleeve is one bet on the market, not
    five independent trades.
    """
    limits = limits or RiskLimits()
    book = list(open_positions) + ([proposed] if proposed else [])
    out: list[Finding] = []
    if not book:
        return out

    if len(book) > limits.max_open_positions:
        out.append(Finding(
            WARN, "too_many_positions",
            f"{len(book)} open positions exceeds the "
            f"{limits.max_open_positions} limit",
        ))

    total = sum(p.get("capital", 0.0) for p in book)
    pct = total / limits.account_value if limits.account_value else 0.0
    if pct > limits.max_total_capital_pct:
        out.append(Finding(
            WARN, "over_committed",
            f"${total:,.0f} committed is {pct:.0%} of the account, above the "
            f"{limits.max_total_capital_pct:.0%} limit",
        ))

    counts: dict[str, int] = {}
    for position in book:
        symbol = str(position.get("symbol", "")).upper()
        counts[symbol] = counts.get(symbol, 0) + 1
    for symbol, count in counts.items():
        if count > limits.max_positions_per_symbol:
            out.append(Finding(
                WARN, "symbol_concentration",
                f"{count} separate positions on {symbol}; the limit is "
                f"{limits.max_positions_per_symbol}",
            ))

    sectors: dict[str, list[str]] = {}
    for position in book:
        sector = position.get("sector")
        if sector:
            sectors.setdefault(sector, []).append(str(position.get("symbol", "")))
    for sector, symbols in sectors.items():
        if len(symbols) > limits.max_positions_per_sector:
            out.append(Finding(
                WARN, "sector_concentration",
                f"{len(symbols)} positions in {sector} "
                f"({', '.join(sorted(symbols))}) - these move together",
            ))

    return _sort(out)


# ---------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------


def check_position(
    *,
    symbol: str,
    right: str,
    strike: float,
    spot: float,
    expiration: date,
    dte: int,
    delta: float,
    entry_credit: float,
    current_mid: float,
    underlying_move_1d: float = float("nan"),
    limits: RiskLimits | None = None,
) -> list[Finding]:
    """Alerts for a position that is already open.

    Silence is the point: a monitor that prints a table every day is one you
    stop reading, and these positions drifted unwatched for two weeks.
    """
    limits = limits or RiskLimits()
    out: list[Finding] = []
    abs_delta = abs(delta) if delta == delta else float("nan")

    itm = (right == "P" and spot < strike) or (right == "C" and spot > strike)
    if itm:
        distance = abs(spot - strike) / strike
        # Anything a cent in the money at the close is auto-exercised, so on
        # expiry day the outcome is settled; before that it is a probability,
        # and delta is the best estimate of it available here.
        certain = dte <= 0
        odds = abs_delta if abs_delta == abs_delta else float("nan")

        if certain:
            level = URGENT
            verdict = "assignment is now certain"
        else:
            level = (
                URGENT
                if (odds == odds and odds >= limits.urgent_assign_prob)
                or dte <= limits.gamma_window_dte
                else WARN
            )
            verdict = (
                f"~{odds:.0%} chance of assignment"
                if odds == odds
                else "assignment likely"
            )
            if odds == odds and odds < 0.99:
                verdict += f", {1 - odds:.0%} it recovers"

        out.append(Finding(
            level, "in_the_money",
            f"{symbol} ${strike:g}{right} {distance:.1%} ITM, {dte} DTE - {verdict}",
        ))

    if abs_delta == abs_delta and abs_delta >= limits.alert_delta and not itm:
        out.append(Finding(
            WARN, "delta_drift",
            f"{symbol} ${strike:g}{right} still OTM but delta {abs_delta:.2f} "
            f"- ~{abs_delta:.0%} chance of assignment, {dte} DTE",
        ))

    if entry_credit > 0 and current_mid >= 0:
        captured = (entry_credit - current_mid) / entry_credit
        if captured >= limits.profit_target_pct:
            out.append(Finding(
                INFO, "profit_target",
                f"{captured:.0%} of max profit captured - closing costs "
                f"${current_mid:.2f} and frees the capital",
            ))

    if dte <= limits.gamma_window_dte and not itm and abs_delta == abs_delta:
        if abs_delta > 0.15:
            out.append(Finding(
                WARN, "gamma_window",
                f"{symbol} ${strike:g}{right} {dte} DTE at {abs_delta:.2f} delta "
                f"- ~{abs_delta:.0%} assignment odds, and they swing fast now",
            ))

    if (
        underlying_move_1d == underlying_move_1d
        and abs(underlying_move_1d) >= limits.underlying_move_alert
    ):
        out.append(Finding(
            WARN, "underlying_moved",
            f"{symbol} moved {underlying_move_1d:+.1%} in a day",
        ))

    return _sort(out)

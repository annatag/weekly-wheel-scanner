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

from .correlation import group_for, groups_for
from .tradingdays import deadline_for_dte

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
    # Relative, for the same reason as the scanner's: an absolute floor
    # screens on share price rather than on what the trade pays.
    min_credit_pct_of_strike: float = 0.003
    min_credit_per_share: float = 0.05
    max_spread_pct: float = 0.12
    require_otm: bool = True
    block_earnings_before_expiry: bool = True
    blocked_setups: tuple[str, ...] = ("falling knife",)

    # --- sizing -------------------------------------------------------
    account_value: float = 100_000.0
    # Most that one adverse two-sigma move may cost, as a share of account.
    risk_budget_pct: float = 0.02
    max_contracts_per_position: int = 10
    # One position may secure the sleeve, and the sleeve is set by the most
    # expensive stock the universe admits: $220 x 100 = $22,000, which is 22%
    # of a $100,000 account. Raising the price ceiling and leaving this at 15%
    # would have the scanner recommend trades the gate then refuses to size.
    max_capital_per_position_pct: float = 0.22

    # --- portfolio ----------------------------------------------------
    # A $22,000 sleeve and a 60% total-capital cap fit two full-size positions
    # and a third smaller one. Eight was arithmetic left over from a $15,000
    # sleeve; leaving it there would have described a diversification the
    # account can no longer hold.
    max_open_positions: int = 3
    max_total_capital_pct: float = 0.60
    max_positions_per_symbol: int = 1
    max_positions_per_sector: int = 3
    # Correlation groups catch what the sector string cannot: ETFs carry no
    # sector, so GDX and SLV counted as two unrelated names while being one
    # bet on the gold price.
    max_positions_per_group: int = 2
    max_group_capital_pct: float = 0.20
    # Correlation groups only catch pairs someone wrote down. Beta catches the
    # exposure nobody labelled: three positions in three unrelated sectors,
    # each with a beta near 2, is one leveraged bet on the index. Capital says
    # they are diverse; beta-weighted capital says otherwise. Set above
    # max_total_capital_pct so it binds on leverage, not on size.
    max_beta_weighted_pct: float = 0.75

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
    # Asymmetric time stop. At the checkpoint a trade that has not paid its
    # way yet is the one to decide about; one already past the threshold is
    # working and gets left alone. "Close everything at N DTE" would have
    # closed a position sitting on 66% of max profit for no reason.
    time_stop_dte: int = 21
    time_stop_min_capture: float = 0.35


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
            f"${credit_per_share:.2f} per share is a tick or two of premium, "
            f"below the ${limits.min_credit_per_share:.2f} floor - the spread "
            "takes it back on the way out",
        ))
    elif strike > 0:
        pct = credit_per_share / strike
        if pct < limits.min_credit_pct_of_strike:
            out.append(Finding(
                WARN, "credit_too_thin",
                f"${credit_per_share:.2f} on a ${strike:g} strike is {pct:.2%} "
                f"of the capital at risk, below the "
                f"{limits.min_credit_pct_of_strike:.2%} floor",
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
    cash: float = float("nan"),
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

    # Equity held counts toward exposure but is not a "position" in the sense
    # this limit means. Selling three puts is three decisions; holding shares
    # from an assignment, or bought outright, is one holding that the caps
    # below still have to see. Counting it here would make the position limit
    # fire on a book that has not opened anything.
    trades = [p for p in book if p.get("kind", "option") != "stock"]
    if len(trades) > limits.max_open_positions:
        out.append(Finding(
            WARN, "too_many_positions",
            f"{len(trades)} open positions exceeds the "
            f"{limits.max_open_positions} limit",
        ))

    total = sum(p.get("capital", 0.0) for p in book)
    pct = total / limits.account_value if limits.account_value else 0.0
    if pct > limits.max_total_capital_pct:
        equity = sum(
            p.get("capital", 0.0) for p in book if p.get("kind") == "stock"
        )
        note = f", of which ${equity:,.0f} is stock held" if equity else ""
        out.append(Finding(
            WARN, "over_committed",
            f"${total:,.0f} committed is {pct:.0%} of the account, above the "
            f"{limits.max_total_capital_pct:.0%} limit{note}",
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

    out.extend(_correlation_findings(book, limits))
    out.extend(_beta_findings(book, limits))
    out.extend(_collateral_findings(book, cash))
    return _sort(out)


def _collateral_findings(book: list[dict], cash: float) -> list[Finding]:
    """Whether the short options are actually secured by cash.

    The strategy is described as selling puts against cash set aside, and the
    sizing model reserves strike x 100 per contract on that basis. An account
    can hold the same positions on margin, where the contracts are identical
    and the risk is not: assignment draws on borrowing rather than on money
    already earmarked, and a drawdown can force the position closed at the
    worst moment rather than simply converting to stock.

    Reported, never blocked. Which way to run the account is not a decision
    this file gets to make - but the trade card should not say "collateral"
    while implying cash that is not there.
    """
    if cash != cash:
        return []
    committed = sum(
        p.get("capital", 0.0) for p in book if p.get("kind", "option") != "stock"
    )
    if committed <= 0 or cash >= committed:
        return []

    shortfall = committed - cash
    return [Finding(
        WARN, "margin_secured",
        f"${committed:,.0f} of collateral against ${cash:,.0f} settled cash - "
        f"${shortfall:,.0f} of these puts is margin-secured, not cash-secured. "
        f"Assignment would draw on borrowing, and the sizing model assumes "
        f"otherwise",
    )]


def _beta_findings(book: list[dict], limits: RiskLimits) -> list[Finding]:
    """Exposure re-expressed in index-equivalent terms.

    Positions without a beta are counted at their face capital rather than
    skipped, so an unmeasurable name cannot quietly shrink the total. The
    finding says how many were assumed.
    """
    if limits.account_value <= 0:
        return []

    weighted = 0.0
    assumed = 0
    for position in book:
        capital = float(position.get("capital", 0.0) or 0.0)
        value = position.get("beta")
        try:
            factor = float(value)
        except (TypeError, ValueError):
            factor = float("nan")
        if factor != factor:
            factor = 1.0
            assumed += 1
        weighted += capital * abs(factor)

    pct = weighted / limits.account_value
    if pct <= limits.max_beta_weighted_pct:
        return []

    note = f" ({assumed} without a beta, counted at 1.0)" if assumed else ""
    return [Finding(
        WARN, "beta_weighted_exposure",
        f"${weighted:,.0f} of index-equivalent exposure is {pct:.0%} of the "
        f"account, above the {limits.max_beta_weighted_pct:.0%} limit{note} - "
        f"the book is more concentrated on the market than its capital suggests",
    )]


def _correlation_findings(book: list[dict], limits: RiskLimits) -> list[Finding]:
    """Exposure aggregated by correlation group rather than by sector label.

    Counted separately from the sector check because the two catch different
    things and a name can trip both: the sector string is what the data feed
    says the company does, and the group is what the position actually bets
    on. An unclassified ticker is constrained by neither, which is the honest
    outcome - the table simply has nothing to say about it.
    """
    out: list[Finding] = []
    counts: dict[str, list[str]] = {}
    capital: dict[str, float] = {}
    for position in book:
        symbol = str(position.get("symbol", "")).upper()
        for group in groups_for(symbol):
            counts.setdefault(group, []).append(symbol)
            capital[group] = capital.get(group, 0.0) + float(
                position.get("capital", 0.0) or 0.0
            )

    for group in sorted(counts):
        symbols = counts[group]
        if len(symbols) > limits.max_positions_per_group:
            out.append(Finding(
                WARN, "correlated_exposure",
                f"{len(symbols)} positions in {group} "
                f"({', '.join(sorted(set(symbols)))}) - one bet, "
                f"{len(symbols)} tickers; the limit is "
                f"{limits.max_positions_per_group}",
            ))
        committed = capital.get(group, 0.0)
        pct = committed / limits.account_value if limits.account_value else 0.0
        if pct > limits.max_group_capital_pct and len(symbols) > 1:
            out.append(Finding(
                WARN, "correlated_capital",
                f"${committed:,.0f} ({pct:.0%} of the account) is committed to "
                f"{group} across {', '.join(sorted(set(symbols)))}, above the "
                f"{limits.max_group_capital_pct:.0%} limit",
            ))
    return out


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
    entry_date: date | None = None,
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

    captured = float("nan")
    if entry_credit > 0 and current_mid >= 0:
        captured = (entry_credit - current_mid) / entry_credit
        if captured >= limits.profit_target_pct:
            out.append(Finding(
                INFO, "profit_target",
                f"{captured:.0%} of max profit captured - closing costs "
                f"${current_mid:.2f} and frees the capital",
            ))

    out.extend(_time_stop(
        symbol=symbol, right=right, strike=strike, expiration=expiration,
        dte=dte, captured=captured, current_mid=current_mid,
        entry_date=entry_date, limits=limits,
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


def checkpoint_dte_for(
    expiration: date, entry_date: date | None, limits: RiskLimits
) -> int:
    """How many days before expiry the position has to justify itself.

    Half the contract's life, capped at ``time_stop_dte``. The cap is what
    matters on a 45-day trade; the half is what matters on the 7-21 day
    contracts this scanner actually sells, where a flat 21-DTE checkpoint is
    already in the past on the day you open. This is the same rule the trade
    card prints as the roll date, so the monitor and the plan agree.

    Without an entry date the original life is unknown and the flat cap is all
    there is. Add an ``entry_date`` column to positions.csv to get the better
    answer.
    """
    if entry_date is None:
        return limits.time_stop_dte
    original_dte = max((expiration - entry_date).days, 0)
    return min(limits.time_stop_dte, max(limits.gamma_window_dte + 1,
                                         original_dte // 2))


def _time_stop(
    *,
    symbol: str,
    right: str,
    strike: float,
    expiration: date,
    dte: int,
    captured: float,
    current_mid: float,
    limits: RiskLimits,
    entry_date: date | None = None,
    today: date | None = None,
) -> list[Finding]:
    """Flag a position that has not paid its way by the checkpoint.

    Deliberately asymmetric. A flat "close everything at N DTE" rule treats
    the trade earning 66% of its credit the same as the one earning 12%, and
    the first of those needs no decision from anyone. Only the laggards are
    raised, so the alert stays worth reading: the position has had most of its
    life to work, the remaining credit is small, and from here gamma grows
    faster than theta pays.

    The deadline is a real session. ``expiration - N days`` lands on a weekend
    two weeks in seven, and a deadline nobody can act on is met late by
    definition.
    """
    if limits.time_stop_dte <= 0 or dte < 0:
        return []
    today = today or date.today()
    checkpoint_dte = checkpoint_dte_for(expiration, entry_date, limits)
    deadline = deadline_for_dte(expiration, checkpoint_dte)
    if today < deadline:
        return []
    # Inside the gamma window the in-the-money and gamma checks already speak,
    # and by then the decision is not "is this working" but "am I assigned".
    if dte <= limits.gamma_window_dte:
        return []
    if captured != captured:
        return [Finding(
            INFO, "time_stop_unknown",
            f"{symbol} ${strike:g}{right} is past its {checkpoint_dte}-DTE "
            f"checkpoint ({deadline:%b %d}) and there is no entry credit on "
            "file to judge it against",
        )]
    if captured >= limits.time_stop_min_capture:
        return []

    shortfall = limits.time_stop_min_capture - captured
    return [Finding(
        WARN, "time_stop",
        f"{symbol} ${strike:g}{right} has captured {captured:.0%} of its credit "
        f"by the {checkpoint_dte}-DTE checkpoint ({deadline:%b %d}), "
        f"{shortfall:.0%} short of the {limits.time_stop_min_capture:.0%} floor "
        f"- close, roll out, or decide to hold it deliberately "
        f"(buyback ${current_mid:.2f}, {dte} DTE left)",
    )]

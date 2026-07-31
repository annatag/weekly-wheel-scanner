"""Limit-price construction and trade management plans.

Nothing here places an order. It produces the exact numbers to type into a
broker ticket, plus the exit rules that decide how long the position is held.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .strategy import Candidate

# US option quoting increments: a penny below $3.00, a nickel at or above it.
# Rounding to the wrong increment produces a limit the exchange will reject.
PENNY_THRESHOLD = 3.00


def tick_size(price: float) -> float:
    return 0.01 if price < PENNY_THRESHOLD else 0.05


def round_to_tick(price: float, mode: str = "nearest") -> float:
    tick = Decimal(str(tick_size(price)))
    value = Decimal(str(max(price, 0.0)))
    rounding = ROUND_DOWN if mode == "down" else ROUND_HALF_UP
    return float((value / tick).quantize(Decimal("1"), rounding=rounding) * tick)


@dataclass(frozen=True)
class LimitPlan:
    """A three-step ladder for working a credit order down toward the mid."""

    open_at: float  # patient opening limit, near the ask
    likely_fill: float  # the mid, where liquid options usually trade
    walk_floor: float  # lowest acceptable credit; cancel rather than go below
    credit_open: float
    credit_likely: float
    credit_floor: float
    contracts: int


def build_limit_plan(candidate: Candidate) -> LimitPlan:
    """Ladder from near-ask down to just below the mid.

    Starting at the ask maximises credit but rarely fills; starting at the mid
    leaves money on the table. Opening ~70% of the way from mid to ask and
    walking down captures most of the spread on liquid contracts.
    """
    mid, bid, ask = candidate.mid, candidate.bid, candidate.ask
    open_at = round_to_tick(mid + 0.70 * (ask - mid), "down")
    likely = round_to_tick(mid, "down")
    floor = round_to_tick(mid - 0.15 * (mid - bid), "down")

    # Guarantee a strictly decreasing, tick-valid ladder even on 1-tick spreads.
    step = tick_size(mid)
    open_at = max(open_at, likely)
    floor = max(min(floor, likely), round_to_tick(bid, "down"))
    if open_at <= likely:
        open_at = round_to_tick(min(likely + step, ask), "down")

    multiplier = 100.0 * candidate.contracts
    return LimitPlan(
        open_at=open_at,
        likely_fill=likely,
        walk_floor=floor,
        credit_open=open_at * multiplier,
        credit_likely=likely * multiplier,
        credit_floor=floor * multiplier,
        contracts=candidate.contracts,
    )


@dataclass(frozen=True)
class ManagementPlan:
    """When to close, when to roll, and what assignment would cost."""

    profit_target_pct: float
    buyback_price: float
    profit_at_target: float
    roll_date: date
    roll_dte: int
    assignment_cost: float
    assignment_note: str
    stop_note: str


def build_management_plan(
    candidate: Candidate, *, profit_target_pct: float = 0.50
) -> ManagementPlan:
    """Standard wheel management, sized to this specific contract.

    Closing at half the maximum profit is the classic rule: the remaining
    credit decays more slowly than the gamma risk grows, so the return per day
    of risk is highest in the first half of the trade.
    """
    buyback = round_to_tick(candidate.mid * (1 - profit_target_pct))
    multiplier = 100.0 * candidate.contracts
    profit = (candidate.mid - buyback) * multiplier

    # Roll or close at 21 DTE, where gamma begins to dominate theta.
    roll_dte = min(21, max(3, candidate.dte // 2))
    roll_date = candidate.expiration - timedelta(days=roll_dte)

    if candidate.right == "P":
        cost = candidate.strike * multiplier
        note = (
            f"Assignment buys {int(candidate.contracts * 100)} shares at "
            f"${candidate.strike:,.2f} for ${cost:,.0f}, "
            f"an effective basis of ${candidate.breakeven:,.2f}."
        )
        stop = (
            f"Consider rolling down and out if the stock closes below "
            f"${candidate.breakeven:,.2f} (your breakeven)."
        )
    else:
        cost = candidate.strike * multiplier
        note = (
            f"Assignment sells {int(candidate.contracts * 100)} shares at "
            f"${candidate.strike:,.2f} for ${cost:,.0f}, "
            f"an effective exit of ${candidate.breakeven:,.2f}."
        )
        stop = (
            f"If the stock runs above ${candidate.breakeven:,.2f} you keep the "
            "credit but cap the upside; roll up and out to stay in the shares."
        )

    return ManagementPlan(
        profit_target_pct=profit_target_pct,
        buyback_price=buyback,
        profit_at_target=profit,
        roll_date=roll_date,
        roll_dte=roll_dte,
        assignment_cost=cost,
        assignment_note=note,
        stop_note=stop,
    )

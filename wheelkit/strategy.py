"""Candidate construction, filtering and scoring for wheel trades.

Scoring changes versus the original model, and why:

* Implied volatility is scored *relative to realised volatility* rather than on
  an absolute scale. The old ``(iv - 0.15) / 0.45`` simply ranked by beta, so
  the same handful of high-volatility names won every week whether or not
  their options were expensive that week.
* Downside cushion is measured in standard deviations of the expected move
  rather than raw dollars. A $5 cushion is generous on a calm name and
  meaningless on a volatile one; the old ``cushion * 1000`` term could not tell
  the difference.
* The underlying-quality term uses dollar volume on a log scale. The old
  ``45 + 12 * log10(...)`` compressed every liquid name into a two-point band,
  so a quarter of the total weight did no ranking work at all.
* Market conditions are derived from SPY instead of the hard-coded 60.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from .analytics import UnderlyingStats, variance_risk_premium
from .pricing import compute_greeks, expected_move, implied_vol
from .providers import Quote

DAYS_PER_YEAR = 365.0


@dataclass(frozen=True)
class WheelConfig:
    # Sizing: total capital committed per position, filled with multiple
    # contracts when the underlying is cheap enough.
    min_cash: float = 3_000
    max_cash: float = 15_000
    # When set, a single contract that exceeds the sleeve is still returned
    # rather than dropped. The scanner leaves this off so the ranking respects
    # position sizing; the advisor turns it on because the user named the
    # ticker and wants an answer, with the true capital requirement shown.
    allow_single_oversize: bool = False

    min_dte: int = 7
    max_dte: int = 21

    # Wider than the original 0.15-0.20 band, which discarded good trades a
    # few thousandths outside it. Scoring peaks near the middle of the band.
    min_abs_delta: float = 0.10
    max_abs_delta: float = 0.32
    ideal_abs_delta: float = 0.20

    # Liquidity. Open interest is deliberately absent: the free data tier does
    # not report it, and gating on a field that is always None rejects
    # everything.
    max_spread_pct: float = 0.12
    min_option_volume: float = 5
    min_quote_size: float = 1
    min_credit_per_share: float = 0.15

    # Underlying quality.
    min_avg_dollar_volume: float = 20_000_000
    max_abs_move_5d: float = 0.15
    require_above_sma50: bool = False

    # Edge. Selling volatility below what the stock actually realises is a
    # negative-expectancy trade regardless of how the premium looks.
    min_vrp: float = 1.0
    min_annualised_return: float = 0.12

    # Event risk.
    skip_earnings: bool = True
    earnings_buffer_days: int = 1

    risk_free_rate: float = 0.04
    top_n: int = 5
    one_per_symbol: bool = True

    weights: dict[str, float] = field(
        default_factory=lambda: {
            "premium": 25.0,
            "iv_edge": 25.0,
            "safety": 20.0,
            "liquidity": 15.0,
            "quality": 10.0,
            "regime": 5.0,
        }
    )


@dataclass
class Candidate:
    symbol: str
    right: str  # "P" cash-secured put, "C" covered call
    occ_symbol: str
    expiration: date
    dte: int
    strike: float
    spot: float

    bid: float
    ask: float
    mid: float
    spread_pct: float
    option_volume: float
    open_interest: int | None

    iv: float
    delta: float
    theta_per_day: float
    prob_itm: float
    prob_profit: float
    vrp: float

    contracts: int
    capital: float
    credit: float  # total dollars for the whole position
    breakeven: float
    cushion_pct: float
    cushion_sigmas: float
    return_on_capital: float
    annualised_return: float

    trend_score: float
    avg_dollar_volume: float
    rv20: float
    move_5d: float
    support_20d: float
    earnings_date: date | None
    quote_age_note: str

    score: float = 0.0
    subscores: dict[str, float] = field(default_factory=dict)


def _interpolate(x: float, points: list[tuple[float, float]]) -> float:
    """Piecewise-linear mapping through (input, output) breakpoints."""
    if x != x:
        return 0.0
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            span = x1 - x0
            return y0 if span <= 0 else y0 + (y1 - y0) * (x - x0) / span
    return points[-1][1]


def score_premium(candidate: Candidate, cfg: WheelConfig) -> float:
    """Annualised return *in excess of cash*, with diminishing returns.

    Secured cash earns the risk-free rate on its own, so only the premium above
    it compensates for taking equity downside. Scoring the raw annualised
    figure made a 6% put on a fully secured position look worthwhile when
    Treasury bills paid 4% for none of the risk.
    """
    excess = candidate.annualised_return - cfg.risk_free_rate
    if excess != excess or excess <= 0:
        return 0.0
    return min(100.0, 100.0 * (1.0 - math.exp(-excess / 0.35)))


def score_iv_edge(candidate: Candidate) -> float:
    """How much the option market overpays versus recent realised movement.

    Very high readings are discounted rather than rewarded: a ratio above ~2.5
    on a short-dated contract usually means the market has priced a known
    event, and the seller is being paid for real gap risk, not for edge.
    """
    vrp = candidate.vrp
    if vrp != vrp:
        return 35.0  # unknown, treat as neutral-low rather than disqualifying
    base = _interpolate(
        vrp,
        [(0.85, 0.0), (1.0, 25.0), (1.15, 50.0), (1.4, 78.0), (1.8, 100.0)],
    )
    if vrp > 2.5:
        base *= 0.75
    return min(100.0, base)


def score_liquidity(candidate: Candidate, cfg: WheelConfig) -> float:
    spread = _interpolate(
        candidate.spread_pct,
        [(0.01, 100.0), (0.03, 85.0), (0.06, 60.0), (cfg.max_spread_pct, 25.0),
         (cfg.max_spread_pct * 1.5, 0.0)],
    )
    volume = _interpolate(
        candidate.option_volume, [(0.0, 0.0), (25.0, 45.0), (200.0, 80.0), (1000.0, 100.0)]
    )
    if candidate.open_interest is not None:
        interest = _interpolate(
            float(candidate.open_interest),
            [(0.0, 0.0), (250.0, 50.0), (1500.0, 85.0), (5000.0, 100.0)],
        )
        return 0.5 * spread + 0.25 * volume + 0.25 * interest
    return 0.65 * spread + 0.35 * volume


def score_safety(candidate: Candidate, cfg: WheelConfig) -> float:
    """Cushion in standard deviations, adjusted for trend and structure."""
    cushion = _interpolate(
        candidate.cushion_sigmas,
        [(0.0, 0.0), (0.75, 45.0), (1.25, 70.0), (2.0, 92.0), (3.0, 100.0)],
    )

    # Reward a breakeven that sits beyond recent price structure.
    structural = 0.0
    if candidate.support_20d == candidate.support_20d and candidate.support_20d > 0:
        if candidate.right == "P" and candidate.breakeven < candidate.support_20d:
            structural = 12.0
        elif candidate.right == "C" and candidate.breakeven > candidate.support_20d:
            structural = 6.0

    # A short put wants a stable-to-rising underlying; a covered call is
    # comfortable with a flat one, so the trend term is halved for calls.
    trend = candidate.trend_score if candidate.right == "P" else 50.0 + (candidate.trend_score - 50.0) * 0.5

    # Being near the ideal delta is itself a safety property.
    delta_fit = _interpolate(
        abs(abs(candidate.delta) - cfg.ideal_abs_delta),
        [(0.0, 100.0), (0.05, 80.0), (0.10, 55.0), (0.20, 20.0)],
    )

    return max(
        0.0, min(100.0, 0.50 * cushion + 0.25 * trend + 0.25 * delta_fit + structural)
    )


def score_quality(candidate: Candidate) -> float:
    """Log-scaled dollar volume: $1M/day scores 0, $10B/day scores 100."""
    volume = candidate.avg_dollar_volume
    if volume != volume or volume <= 0:
        return 0.0
    return max(0.0, min(100.0, 25.0 * (math.log10(volume) - 6.0)))


def score_candidate(candidate: Candidate, cfg: WheelConfig, regime: float) -> Candidate:
    subscores = {
        "premium": score_premium(candidate, cfg),
        "iv_edge": score_iv_edge(candidate),
        "safety": score_safety(candidate, cfg),
        "liquidity": score_liquidity(candidate, cfg),
        "quality": score_quality(candidate),
        "regime": regime,
    }
    total_weight = sum(cfg.weights.values()) or 1.0
    candidate.subscores = subscores
    candidate.score = round(
        sum(subscores[k] * w for k, w in cfg.weights.items()) / total_weight, 1
    )
    return candidate


def size_position(strike: float, cfg: WheelConfig) -> tuple[int, float] | None:
    """Contracts that fit the cash sleeve, allowing multiples on cheap names.

    The original scanner derived a minimum strike from the cash floor, which
    silently excluded every stock trading below ~$71 no matter how well it
    suited the strategy.
    """
    per_contract = strike * 100.0
    if per_contract <= 0:
        return None
    if per_contract > cfg.max_cash:
        return (1, per_contract) if cfg.allow_single_oversize else None
    contracts = int(cfg.max_cash // per_contract)
    if contracts < 1:
        return None
    capital = contracts * per_contract
    if capital < cfg.min_cash:
        return None
    return contracts, capital


def _quote_age_note(quote: Quote, market_open: bool) -> str:
    if quote.quote_time is None:
        return "no timestamp"
    from datetime import datetime, timezone

    age = (datetime.now(timezone.utc) - quote.quote_time).total_seconds()
    if not market_open:
        return "last close"
    if age > 900:
        return f"stale {age / 60:.0f}m"
    if age > 180:
        return f"{age / 60:.0f}m old"
    return "fresh"


def build_candidates(
    symbol: str,
    quotes: list[Quote],
    stats: UnderlyingStats,
    cfg: WheelConfig,
    *,
    right: str,
    today: date,
    earnings_date: date | None = None,
    market_open: bool = False,
    shares_held: float = 0.0,
    cost_basis: float = 0.0,
    stats_counter: Counter[str] | None = None,
) -> list[Candidate]:
    """Turn raw quotes into filtered, fully-costed candidates."""
    rejects = stats_counter if stats_counter is not None else Counter()
    results: list[Candidate] = []
    spot = stats.spot

    for quote in quotes:
        if quote.right != right:
            continue

        dte = (quote.expiration - today).days
        if not cfg.min_dte <= dte <= cfg.max_dte:
            rejects["dte outside band"] += 1
            continue

        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            rejects["no two-sided quote"] += 1
            continue
        if quote.spread_pct > cfg.max_spread_pct:
            rejects["spread too wide"] += 1
            continue
        if quote.mid < cfg.min_credit_per_share:
            rejects["credit below floor"] += 1
            continue
        if quote.volume < cfg.min_option_volume:
            rejects["option volume too low"] += 1
            continue
        if min(quote.bid_size, quote.ask_size) < cfg.min_quote_size:
            rejects["quote size too thin"] += 1
            continue

        if earnings_date is not None and cfg.skip_earnings:
            cutoff = quote.expiration
            if today <= earnings_date <= cutoff:
                rejects["earnings before expiry"] += 1
                continue

        time_to_expiry = dte / DAYS_PER_YEAR
        iv = implied_vol(
            quote.mid, spot, quote.strike, time_to_expiry, cfg.risk_free_rate, right=right
        )
        if iv is None:
            rejects["implied vol unsolvable"] += 1
            continue

        greeks = compute_greeks(
            spot, quote.strike, time_to_expiry, iv, quote.mid, cfg.risk_free_rate, right=right
        )
        if greeks is None:
            rejects["greeks unavailable"] += 1
            continue
        if not cfg.min_abs_delta <= abs(greeks.delta) <= cfg.max_abs_delta:
            rejects["delta outside band"] += 1
            continue

        vrp = variance_risk_premium(iv, stats.rv20, stats.rv60)
        if vrp == vrp and vrp < cfg.min_vrp:
            rejects["implied vol below realised"] += 1
            continue

        # Sizing differs by leg: puts consume cash, calls consume shares.
        if right == "P":
            sized = size_position(quote.strike, cfg)
            if sized is None:
                rejects["outside cash sleeve"] += 1
                continue
            contracts, capital = sized
            breakeven = quote.strike - quote.mid
            cushion_pct = (spot - breakeven) / spot if spot > 0 else float("nan")
        else:
            contracts = int(shares_held // 100)
            if contracts < 1:
                rejects["fewer than 100 shares held"] += 1
                continue
            capital = contracts * 100.0 * spot
            breakeven = quote.strike + quote.mid
            cushion_pct = (breakeven - spot) / spot if spot > 0 else float("nan")
            # Never write a call that would force a sale below your basis.
            if cost_basis > 0 and quote.strike < cost_basis:
                rejects["strike below cost basis"] += 1
                continue

        credit = quote.mid * 100.0 * contracts
        return_on_capital = credit / capital if capital > 0 else float("nan")
        annualised = return_on_capital * DAYS_PER_YEAR / dte if dte > 0 else float("nan")
        if annualised != annualised or annualised < cfg.min_annualised_return:
            rejects["annualised return below floor"] += 1
            continue

        sigma = expected_move(spot, iv, time_to_expiry)
        cushion_sigmas = abs(spot - breakeven) / sigma if sigma > 0 else float("nan")

        results.append(
            Candidate(
                symbol=symbol,
                right=right,
                occ_symbol=quote.occ_symbol,
                expiration=quote.expiration,
                dte=dte,
                strike=quote.strike,
                spot=spot,
                bid=quote.bid,
                ask=quote.ask,
                mid=quote.mid,
                spread_pct=quote.spread_pct,
                option_volume=quote.volume,
                open_interest=quote.open_interest,
                iv=iv,
                delta=greeks.delta,
                theta_per_day=greeks.theta_per_day * 100 * contracts,
                prob_itm=greeks.prob_itm,
                prob_profit=greeks.prob_profit,
                vrp=vrp,
                contracts=contracts,
                capital=capital,
                credit=credit,
                breakeven=breakeven,
                cushion_pct=cushion_pct,
                cushion_sigmas=cushion_sigmas,
                return_on_capital=return_on_capital,
                annualised_return=annualised,
                trend_score=stats.trend_score,
                avg_dollar_volume=stats.avg_dollar_volume,
                rv20=stats.rv20,
                move_5d=stats.move_5d,
                support_20d=stats.support_20d,
                earnings_date=earnings_date,
                quote_age_note=_quote_age_note(quote, market_open),
            )
        )
        rejects["accepted"] += 1

    return results


def resize_candidate(candidate: Candidate, contracts: int) -> Candidate:
    """Force a candidate to a specific contract count and restate the totals.

    Used when re-quoting a saved trade, where the position was already sized
    and only the price has moved. Re-running the sizing rules instead would
    silently rewrite the trade to fit whatever sleeve the caller configured.
    """
    if contracts < 1:
        return candidate
    scale = contracts / candidate.contracts if candidate.contracts else 1.0
    per_contract = (
        candidate.strike * 100.0 if candidate.right == "P" else candidate.spot * 100.0
    )

    candidate.theta_per_day *= scale
    candidate.contracts = contracts
    candidate.capital = contracts * per_contract
    candidate.credit = candidate.mid * 100.0 * contracts
    candidate.return_on_capital = (
        candidate.credit / candidate.capital if candidate.capital > 0 else float("nan")
    )
    candidate.annualised_return = (
        candidate.return_on_capital * DAYS_PER_YEAR / candidate.dte
        if candidate.dte > 0
        else float("nan")
    )
    return candidate


def underlying_passes(
    stats: UnderlyingStats, cfg: WheelConfig, right: str
) -> str | None:
    """Underlying-level gate. Returns a rejection reason, or None to proceed."""
    if stats.avg_dollar_volume < cfg.min_avg_dollar_volume:
        return "dollar volume below minimum"
    if stats.move_5d == stats.move_5d and abs(stats.move_5d) > cfg.max_abs_move_5d:
        return "five-day move outside limit"
    if right == "P" and cfg.require_above_sma50:
        if stats.sma50 == stats.sma50 and stats.spot < stats.sma50:
            return "below 50-day average"
    return None


def rank(candidates: list[Candidate], cfg: WheelConfig) -> list[Candidate]:
    """Best-first, optionally one contract per symbol for diversification.

    Without the per-symbol cap the top five can be five strikes on the same
    ticker, which concentrates rather than diversifies the week's risk.
    """
    ordered = sorted(
        candidates, key=lambda c: (c.score, c.annualised_return), reverse=True
    )
    if not cfg.one_per_symbol:
        return ordered[: cfg.top_n]

    seen: set[str] = set()
    picked: list[Candidate] = []
    for candidate in ordered:
        if candidate.symbol in seen:
            continue
        seen.add(candidate.symbol)
        picked.append(candidate)
        if len(picked) >= cfg.top_n:
            break
    return picked

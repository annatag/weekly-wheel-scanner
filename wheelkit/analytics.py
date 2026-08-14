"""Underlying statistics that decide whether a name is worth selling against.

The original scanner scored implied volatility on an absolute scale, so a
permanently volatile name like PLTR always outranked a calm one regardless of
whether its options were actually expensive that week. What matters for a
premium seller is the *variance risk premium*: implied volatility relative to
what the stock has recently been doing. That is computable from free data,
whereas a true IV rank would need a year of historical option surfaces.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .providers import Bar

TRADING_DAYS = 252


@dataclass(frozen=True)
class UnderlyingStats:
    spot: float
    avg_dollar_volume: float
    avg_share_volume: float
    move_5d: float
    move_20d: float
    move_quarter: float  # 63 trading days, matching Finviz's "Performance (Quarter)"
    setup: str  # pullback | momentum | falling knife | rebound | unknown
    rv20: float  # annualised realised volatility, last 20 sessions
    rv60: float
    rv_percentile: float  # where rv20 sits within its own 1-year history
    sma20: float
    sma50: float
    sma200: float
    trend_score: float  # 0-100, higher is a healthier uptrend
    support_20d: float
    support_60d: float
    resistance_20d: float
    atr14_pct: float
    max_drawdown_60d: float
    bars_used: int


def _returns(closes: list[float]) -> list[float]:
    return [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i] > 0 and closes[i - 1] > 0
    ]


def realised_vol(closes: list[float], window: int) -> float:
    """Annualised close-to-close volatility over the trailing window."""
    rets = _returns(closes[-(window + 1):])
    if len(rets) < 5:
        return float("nan")
    return statistics.pstdev(rets) * math.sqrt(TRADING_DAYS)


def parkinson_vol(bars: list[Bar], window: int) -> float:
    """High-low range volatility estimator.

    Roughly five times more efficient than close-to-close for the same sample
    size, which matters on a 20-day window where the naive estimator is noisy.
    """
    recent = [b for b in bars[-window:] if b.high > 0 and b.low > 0 and b.high >= b.low]
    if len(recent) < 5:
        return float("nan")
    factor = 1.0 / (4.0 * math.log(2.0))
    mean_sq = sum(math.log(b.high / b.low) ** 2 for b in recent) / len(recent)
    return math.sqrt(factor * mean_sq * TRADING_DAYS)


def _rv_percentile(closes: list[float], window: int = 20) -> float:
    """Rank today's 20-day realised vol against every other 20-day window."""
    if len(closes) < window + 60:
        return float("nan")
    series = []
    for end in range(window + 1, len(closes) + 1):
        rets = _returns(closes[end - window - 1: end])
        if len(rets) >= window - 1:
            series.append(statistics.pstdev(rets))
    if len(series) < 30:
        return float("nan")
    current = series[-1]
    return 100.0 * sum(1 for value in series if value <= current) / len(series)


def _sma(values: list[float], window: int) -> float:
    if len(values) < window:
        return float("nan")
    return sum(values[-window:]) / window


def average_true_range(bars: list[Bar], window: int = 14) -> float:
    if len(bars) < window + 1:
        return float("nan")
    trs = []
    for prev, cur in zip(bars[-(window + 1):-1], bars[-window:]):
        trs.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    return sum(trs) / len(trs) if trs else float("nan")


PULLBACK = "pullback"
MOMENTUM = "momentum"
FALLING_KNIFE = "falling knife"
REBOUND = "rebound"
UNKNOWN = "unknown"


def classify_setup(move_quarter: float, move_month: float) -> str:
    """Label the trend from the quarter and month returns.

    The quarter sets the primary direction and the month says where price sits
    within it. A stock up over the quarter but down over the month has an
    intact uptrend and a recent dip: implied volatility is elevated by the
    selloff while the longer trend still supports the strike. That is the
    setup a put seller wants, and it is the one a naive "recent drift"
    penalty rejects hardest.
    """
    if move_quarter != move_quarter or move_month != move_month:
        return UNKNOWN
    if move_quarter > 0:
        return PULLBACK if move_month < 0 else MOMENTUM
    return REBOUND if move_month > 0 else FALLING_KNIFE


# How attractive each setup is to a put seller, 0-100. Scored as its own
# term rather than folded into the trend score: buried inside safety, the
# whole pullback-to-falling-knife range moved the final score under two
# points, which is not enough to change any ranking.
SETUP_SCORES = {
    PULLBACK: 100.0,  # uptrend intact, dip has richened the premium
    MOMENTUM: 55.0,  # healthy but extended, and volatility is usually cheap
    REBOUND: 30.0,  # bouncing inside a downtrend
    FALLING_KNIFE: 0.0,  # down on both horizons
    UNKNOWN: 50.0,  # too little history to judge; stay neutral
}


def setup_score(setup: str) -> float:
    return SETUP_SCORES.get(setup, 50.0)


def _trend_score(spot: float, sma20: float, sma50: float, sma200: float) -> float:
    """Moving-average structure alone, 0-100.

    Previously this also added ``move_20d * 100``, treating every monthly
    decline as a defect and so penalising the pullback setup precisely because
    it had pulled back. The setup now carries its own weight in the score, and
    this term is left to measure structure only, so neither is counted twice.
    """
    score = 50.0
    for average, weight in ((sma20, 12.0), (sma50, 20.0), (sma200, 12.0)):
        if average == average and average > 0:
            score += weight if spot > average else -weight
    if sma50 == sma50 and sma200 == sma200 and sma50 > 0 and sma200 > 0:
        score += 6.0 if sma50 > sma200 else -6.0
    return max(0.0, min(100.0, score))


def compute_stats(bars: list[Bar], spot: float) -> UnderlyingStats | None:
    """Derive every underlying-level input from a daily bar history."""
    if len(bars) < 30:
        return None

    closes = [b.close for b in bars]
    if spot <= 0:
        spot = closes[-1]

    volumes = [b.volume for b in bars[-20:]]
    avg_share_volume = sum(volumes) / len(volumes) if volumes else 0.0
    avg_dollar_volume = (
        sum(b.close * b.volume for b in bars[-20:]) / min(len(bars), 20)
    )

    move_5d = spot / closes[-6] - 1 if len(closes) >= 6 and closes[-6] > 0 else float("nan")
    move_20d = spot / closes[-21] - 1 if len(closes) >= 21 and closes[-21] > 0 else float("nan")
    move_quarter = (
        spot / closes[-64] - 1 if len(closes) >= 64 and closes[-64] > 0 else float("nan")
    )
    setup = classify_setup(move_quarter, move_20d)

    # Prefer the Parkinson estimator, fall back to close-to-close.
    rv20 = parkinson_vol(bars, 20)
    if rv20 != rv20:
        rv20 = realised_vol(closes, 20)
    rv60 = parkinson_vol(bars, 60)
    if rv60 != rv60:
        rv60 = realised_vol(closes, 60)

    sma20, sma50, sma200 = _sma(closes, 20), _sma(closes, 50), _sma(closes, 200)
    lows20 = [b.low for b in bars[-20:]]
    lows60 = [b.low for b in bars[-60:]]
    highs20 = [b.high for b in bars[-20:]]

    peak = max(closes[-60:])
    drawdown = (spot / peak - 1) if peak > 0 else float("nan")
    atr = average_true_range(bars, 14)

    return UnderlyingStats(
        spot=spot,
        avg_dollar_volume=avg_dollar_volume,
        avg_share_volume=avg_share_volume,
        move_5d=move_5d,
        move_20d=move_20d,
        move_quarter=move_quarter,
        setup=setup,
        rv20=rv20,
        rv60=rv60,
        rv_percentile=_rv_percentile(closes),
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        trend_score=_trend_score(spot, sma20, sma50, sma200),
        support_20d=min(lows20) if lows20 else float("nan"),
        support_60d=min(lows60) if lows60 else float("nan"),
        resistance_20d=max(highs20) if highs20 else float("nan"),
        atr14_pct=atr / spot if atr == atr and spot > 0 else float("nan"),
        max_drawdown_60d=drawdown,
        bars_used=len(bars),
    )


def variance_risk_premium(iv: float, rv20: float, rv60: float) -> float:
    """Implied volatility divided by a blended realised volatility baseline.

    Above ~1.15 the option market is charging a genuine premium over recent
    movement, which is the edge a seller is paid for. Below ~1.0 you are being
    paid less than the stock has actually been moving: a bad sale.
    """
    baseline = None
    if rv20 == rv20 and rv60 == rv60:
        baseline = 0.65 * rv20 + 0.35 * rv60
    elif rv20 == rv20:
        baseline = rv20
    elif rv60 == rv60:
        baseline = rv60
    if not baseline or baseline <= 0.01 or iv != iv or iv <= 0:
        return float("nan")
    return iv / baseline


def market_regime(spy_stats: UnderlyingStats | None) -> tuple[float, str]:
    """A 0-100 read on whether conditions favour selling premium.

    Replaces the old hard-coded constant of 60. Derived from SPY alone so it
    needs no index-data entitlement.
    """
    if spy_stats is None:
        return 50.0, "unknown (SPY data unavailable)"

    score = 50.0
    notes = []

    if spy_stats.rv20 == spy_stats.rv20:
        if spy_stats.rv20 < 0.12:
            score += 20
            notes.append("calm tape")
        elif spy_stats.rv20 < 0.20:
            score += 8
            notes.append("normal volatility")
        elif spy_stats.rv20 < 0.30:
            score -= 8
            notes.append("elevated volatility")
        else:
            score -= 25
            notes.append("high volatility - size down")

    if spy_stats.sma50 == spy_stats.sma50 and spy_stats.sma50 > 0:
        if spy_stats.spot > spy_stats.sma50:
            score += 15
            notes.append("SPY above 50-day")
        else:
            score -= 15
            notes.append("SPY below 50-day")

    if spy_stats.max_drawdown_60d == spy_stats.max_drawdown_60d:
        if spy_stats.max_drawdown_60d < -0.10:
            score -= 10
            notes.append("SPY >10% off its 60-day high")

    return max(0.0, min(100.0, score)), ", ".join(notes)

"""Shape of the volatility surface: skew across strikes, slope across expiries.

The screen prices one contract at a time and asks whether it is rich against
what the stock has been doing. That misses two things a premium seller cares
about, both of which live in the *shape* of the surface rather than in any
single quote.

**Skew.** A put seller is short the left tail specifically. Implied volatility
almost always rises as strikes fall, so some skew is normal - but when the
strike you are selling is priced far above the at-the-money level, the premium
you are being paid is compensation for crash risk rather than a general
richness you can harvest. Two contracts with identical VRP are different
trades if one of them sits on a steep wing.

**Term structure.** Front-month implied volatility above back-month is the
signature of a dated event. It is the only measure here that can see a
catalyst the earnings calendar does not list - a court date, an FDA decision,
a deal vote - because the market prices those into the expiry that contains
them and not the one after.

Both are computed from quotes the scan already has, or nearly has, and neither
needs a data subscription.
"""

from __future__ import annotations

from datetime import date

from .pricing import implied_vol

DAYS_PER_YEAR = 365.0


def _quote_iv(quote, spot: float, today: date, risk_free: float) -> float | None:
    """Implied volatility for one quote, or None when it cannot be inverted."""
    dte = (quote.expiration - today).days
    if dte <= 0 or spot <= 0 or quote.mid <= 0:
        return None
    return implied_vol(
        quote.mid, spot, quote.strike, dte / DAYS_PER_YEAR,
        risk_free, right=quote.right,
    )


def atm_iv(
    quotes: list, spot: float, today: date, risk_free: float = 0.04
) -> dict[date, float]:
    """At-the-money implied volatility per expiry, from the nearest strike.

    "Nearest strike" rather than an interpolated forward-ATM: the difference
    is immaterial against the strike spacing of the names this scans, and
    interpolating would invent precision the quotes do not carry.
    """
    nearest: dict[date, tuple[float, object]] = {}
    for quote in quotes:
        if quote.mid <= 0:
            continue
        distance = abs(quote.strike - spot)
        current = nearest.get(quote.expiration)
        if current is None or distance < current[0]:
            nearest[quote.expiration] = (distance, quote)

    out: dict[date, float] = {}
    for expiration, (_, quote) in nearest.items():
        iv = _quote_iv(quote, spot, today, risk_free)
        if iv is not None and iv > 0:
            out[expiration] = iv
    return out


def skew_ratio(strike_iv: float, reference_atm_iv: float) -> float:
    """Strike IV over at-the-money IV for the same expiry.

    1.0 is a flat surface. Around 1.05-1.15 is ordinary equity skew. Well
    above that, the market is paying specifically for downside protection at
    the strike you are selling, and the extra premium is a fee for tail risk
    rather than an edge over realised movement.
    """
    if (
        strike_iv != strike_iv or reference_atm_iv != reference_atm_iv
        or reference_atm_iv <= 0
    ):
        return float("nan")
    return strike_iv / reference_atm_iv


def term_slope(front_atm_iv: float, back_atm_iv: float) -> float:
    """Front-expiry ATM IV over a later expiry's.

    Above ~1.10 is backwardation: the near contract is pricing something the
    far one is not. Below 1.0 is the ordinary upward-sloping term structure of
    a calm name, which is the state a seller wants.
    """
    if (
        front_atm_iv != front_atm_iv or back_atm_iv != back_atm_iv
        or back_atm_iv <= 0
    ):
        return float("nan")
    return front_atm_iv / back_atm_iv

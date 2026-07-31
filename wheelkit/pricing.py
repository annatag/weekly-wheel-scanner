"""Black-Scholes pricing, implied volatility and the probabilities that matter.

The free Alpaca data tier does not return greeks or implied volatility, so we
derive them from the quoted mid price. That is a feature rather than a
workaround: every number below is reproducible from the inputs, with no vendor
smoothing hiding a stale or nonsensical quote.

Short-dated out-of-the-money American options on non-dividend dates behave
close enough to European ones that the early-exercise premium is immaterial
here. Deep in-the-money puts are the exception, and the wheel never sells those.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SQRT_2PI = math.sqrt(2 * math.pi)
DAYS_PER_YEAR = 365.0

# Practical bounds for the implied-volatility solver. A quote implying less
# than 1% or more than 500% annualised vol is a broken quote, not a signal.
MIN_IV = 0.01
MAX_IV = 5.0


@dataclass(frozen=True)
class Greeks:
    """Per-contract risk measures. Theta and vega are already scaled for use."""

    iv: float
    delta: float
    gamma: float
    theta_per_day: float  # dollars per share per calendar day
    vega_per_point: float  # dollars per share per 1 volatility point
    prob_itm: float  # risk-neutral probability of finishing in the money
    prob_profit: float  # probability the short option expires above breakeven


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _d1_d2(
    spot: float, strike: float, t: float, vol: float, r: float, q: float
) -> tuple[float, float]:
    variance = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t) / variance
    return d1, d1 - variance


def black_scholes_price(
    spot: float,
    strike: float,
    t: float,
    vol: float,
    r: float = 0.04,
    q: float = 0.0,
    right: str = "P",
) -> float:
    """Theoretical option value. ``t`` is time to expiry in years."""
    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        intrinsic = strike - spot if right == "P" else spot - strike
        return max(0.0, intrinsic)

    d1, d2 = _d1_d2(spot, strike, t, vol, r, q)
    discount = math.exp(-r * t)
    carry = math.exp(-q * t)
    if right == "P":
        return strike * discount * norm_cdf(-d2) - spot * carry * norm_cdf(-d1)
    return spot * carry * norm_cdf(d1) - strike * discount * norm_cdf(d2)


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    t: float,
    r: float = 0.04,
    q: float = 0.0,
    right: str = "P",
) -> float | None:
    """Invert Black-Scholes for volatility using bisection.

    Bisection rather than Newton-Raphson: vega collapses toward zero for the
    far out-of-the-money strikes the wheel actually sells, which makes Newton
    diverge exactly where we need an answer. Bisection is slower and always
    converges because price is monotonic in volatility.
    """
    # Below half a tick the price carries no volatility information: vega has
    # collapsed, so a wide range of volatilities reprices to the same number
    # and the solver would return an arbitrary point inside it.
    if price < 0.005 or t <= 0 or spot <= 0 or strike <= 0:
        return None

    # Reject quotes outside the no-arbitrage band; they cannot imply any vol.
    intrinsic = max(
        0.0,
        (strike * math.exp(-r * t) - spot * math.exp(-q * t))
        if right == "P"
        else (spot * math.exp(-q * t) - strike * math.exp(-r * t)),
    )
    upper_bound = strike * math.exp(-r * t) if right == "P" else spot * math.exp(-q * t)
    if price <= intrinsic or price >= upper_bound:
        return None

    low, high = MIN_IV, MAX_IV
    if black_scholes_price(spot, strike, t, high, r, q, right) < price:
        return None

    for _ in range(100):
        mid = 0.5 * (low + high)
        if black_scholes_price(spot, strike, t, mid, r, q, right) < price:
            low = mid
        else:
            high = mid
        if high - low < 1e-6:
            break
    return 0.5 * (low + high)


def compute_greeks(
    spot: float,
    strike: float,
    t: float,
    vol: float,
    premium: float,
    r: float = 0.04,
    q: float = 0.0,
    right: str = "P",
) -> Greeks | None:
    """Full risk profile for one short contract, including assignment odds."""
    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return None

    d1, d2 = _d1_d2(spot, strike, t, vol, r, q)
    sqrt_t = math.sqrt(t)
    carry = math.exp(-q * t)
    discount = math.exp(-r * t)
    pdf_d1 = norm_pdf(d1)

    gamma = carry * pdf_d1 / (spot * vol * sqrt_t)
    vega = spot * carry * pdf_d1 * sqrt_t / 100.0
    common_theta = -spot * carry * pdf_d1 * vol / (2 * sqrt_t)

    if right == "P":
        delta = -carry * norm_cdf(-d1)
        theta = (
            common_theta
            + r * strike * discount * norm_cdf(-d2)
            - q * spot * carry * norm_cdf(-d1)
        )
        prob_itm = norm_cdf(-d2)
        breakeven = strike - premium
    else:
        delta = carry * norm_cdf(d1)
        theta = (
            common_theta
            - r * strike * discount * norm_cdf(d2)
            + q * spot * carry * norm_cdf(d1)
        )
        prob_itm = norm_cdf(d2)
        breakeven = strike + premium

    return Greeks(
        iv=vol,
        delta=delta,
        gamma=gamma,
        theta_per_day=theta / DAYS_PER_YEAR,
        vega_per_point=vega,
        prob_itm=prob_itm,
        prob_profit=prob_touch_free(spot, breakeven, t, vol, r, q, right),
    )


def prob_touch_free(
    spot: float,
    breakeven: float,
    t: float,
    vol: float,
    r: float,
    q: float,
    right: str,
) -> float:
    """Probability the short option finishes profitable (terminal, not touch).

    A short put profits when the underlying settles above its breakeven; a
    short call profits below. This ignores the chance of an intra-period breach
    that you close early, which is why it reads higher than realised win rates.
    """
    if breakeven <= 0 or t <= 0 or vol <= 0:
        return float("nan")
    _, d2 = _d1_d2(spot, breakeven, t, vol, r, q)
    return norm_cdf(d2) if right == "P" else norm_cdf(-d2)


def expected_move(spot: float, vol: float, t: float) -> float:
    """One standard deviation of price movement over the holding period."""
    return spot * vol * math.sqrt(t)


def strike_for_delta(
    spot: float,
    target_delta: float,
    t: float,
    vol: float,
    r: float = 0.04,
    q: float = 0.0,
    right: str = "P",
) -> float:
    """Invert delta to a strike, used to focus the chain request on a range.

    Requesting only the strikes that can plausibly hit the target delta band
    turns a 1,000-contract pagination crawl into a single filtered call.
    """
    target = abs(target_delta)
    if not 0 < target < 1 or t <= 0 or vol <= 0:
        return spot

    # Invert N(d1) = target, then solve the d1 definition for the strike.
    d1 = _inverse_norm_cdf(1 - target if right == "P" else target)
    return spot * math.exp(-d1 * vol * math.sqrt(t) + (r - q + 0.5 * vol * vol) * t)


def _inverse_norm_cdf(p: float) -> float:
    """Acklam's rational approximation to the normal quantile function."""
    if not 0 < p < 1:
        return 0.0
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    ]
    low, high = 0.02425, 1 - 0.02425

    if p < low:
        z = math.sqrt(-2 * math.log(p))
        return (((((c[0] * z + c[1]) * z + c[2]) * z + c[3]) * z + c[4]) * z + c[5]) / (
            (((d[0] * z + d[1]) * z + d[2]) * z + d[3]) * z + 1
        )
    if p > high:
        z = math.sqrt(-2 * math.log(1 - p))
        return -(
            ((((c[0] * z + c[1]) * z + c[2]) * z + c[3]) * z + c[4]) * z + c[5]
        ) / ((((d[0] * z + d[1]) * z + d[2]) * z + d[3]) * z + 1)

    z = p - 0.5
    r_val = z * z
    return (
        (((((a[0] * r_val + a[1]) * r_val + a[2]) * r_val + a[3]) * r_val + a[4]) * r_val + a[5])
        * z
        / (((((b[0] * r_val + b[1]) * r_val + b[2]) * r_val + b[3]) * r_val + b[4]) * r_val + 1)
    )

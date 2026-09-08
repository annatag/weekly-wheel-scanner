"""Scan orchestration: fetch, filter and score across a symbol universe."""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from .analytics import UnderlyingStats, compute_stats, market_regime
from .earnings import EarningsCalendar
from .netio import FetchError
from .pricing import strike_for_delta
from .providers import Provider, Quote
from .volsurface import atm_iv, term_slope
from .strategy import (
    Candidate,
    WheelConfig,
    build_candidates,
    fundamentals_pass,
    rank,
    score_candidate,
    underlying_passes,
)

# Widen the requested strike window past the delta band so that a stale
# volatility estimate cannot clip the range before the real filter runs.
STRIKE_PAD = 0.12


@dataclass
class ScanContext:
    regime_score: float
    regime_note: str
    market_open: bool
    earnings_available: bool
    earnings_source: str = "feed"


def prepare_context(provider: Provider, earnings: EarningsCalendar) -> ScanContext:
    """Establish market regime and session state once per run."""
    spy_stats: UnderlyingStats | None = None
    try:
        bars = provider.daily_bars("SPY", 400)
        spot, _ = provider.spot("SPY")
        spy_stats = compute_stats(bars, spot)
    except (FetchError, Exception):
        spy_stats = None

    regime_score, regime_note = market_regime(spy_stats)

    market_open = False
    clock = getattr(provider, "market_clock", None)
    if callable(clock):
        try:
            market_open = bool(clock().get("is_open"))
        except Exception:
            market_open = False

    return ScanContext(
        regime_score=regime_score,
        regime_note=regime_note,
        market_open=market_open,
        earnings_available=earnings.available,
        earnings_source=getattr(earnings, "source", "feed"),
    )


def strike_window(
    spot: float, stats: UnderlyingStats, cfg: WheelConfig, right: str
) -> tuple[float, float]:
    """Strike range that can plausibly contain the target delta band.

    Requesting only this slice turns a multi-page chain crawl into one call.
    """
    vol = stats.rv20 if stats.rv20 == stats.rv20 and stats.rv20 > 0 else 0.35
    # Realised vol understates implied for most names; pad it so the window
    # does not close in on the money and miss the band entirely.
    vol = max(0.12, min(2.0, vol * 1.25))

    near = strike_for_delta(spot, cfg.max_abs_delta, cfg.max_dte / 365, vol, right=right)
    far = strike_for_delta(spot, cfg.min_abs_delta, cfg.min_dte / 365, vol, right=right)
    low, high = min(near, far), max(near, far)
    low, high = low * (1 - STRIKE_PAD), high * (1 + STRIKE_PAD)

    # Reach to the money as well. Skew is only meaningful against an
    # at-the-money reference, and pulling it in the same request costs one
    # widened range rather than a second round trip per symbol. The extra
    # strikes are inside the delta band's near edge, so they are rejected at
    # scoring - into their own bucket, not the band's.
    if right == "P":
        high = max(high, spot * 1.02)
    else:
        low = min(low, spot * 0.98)
    return low, high


def scan_symbol(
    provider: Provider,
    symbol: str,
    cfg: WheelConfig,
    context: ScanContext,
    earnings: EarningsCalendar,
    *,
    right: str = "P",
    today: date | None = None,
    shares_held: float = 0.0,
    cost_basis: float = 0.0,
    rejects: Counter[str] | None = None,
    fundamentals: dict[str, dict[str, float | None]] | None = None,
) -> tuple[list[Candidate], UnderlyingStats | None, str | None]:
    """Scan one symbol. Returns (candidates, stats, skip_reason)."""
    today = today or date.today()
    rejects = rejects if rejects is not None else Counter()

    try:
        bars = provider.daily_bars(symbol, 400)
        spot, _ = provider.spot(symbol)
    except FetchError as exc:
        return [], None, f"data unavailable ({exc})"

    stats = compute_stats(bars, spot)
    if stats is None:
        return [], None, "insufficient price history"

    skip = underlying_passes(stats, cfg, right)
    if skip:
        return [], stats, skip

    # Checked before the chain request so a rejected name costs no API call.
    if right == "P":
        skip = fundamentals_pass(symbol, fundamentals, cfg)
        if skip:
            return [], stats, skip

    earnings_date = earnings.next_date(symbol)
    low, high = strike_window(stats.spot, stats, cfg, right)

    try:
        quotes: list[Quote] = provider.option_chain(
            symbol,
            expiry_from=today + timedelta(days=cfg.min_dte),
            expiry_to=today + timedelta(days=cfg.max_dte),
            strike_min=low,
            strike_max=high,
            right=right,
        )
    except FetchError as exc:
        return [], stats, f"option chain unavailable ({exc})"

    if not quotes:
        return [], stats, "no contracts in the target strike window"

    candidates = build_candidates(
        symbol,
        quotes,
        stats,
        cfg,
        right=right,
        today=today,
        earnings_date=earnings_date,
        market_open=context.market_open,
        shares_held=shares_held,
        cost_basis=cost_basis,
        stats_counter=rejects,
        fundamentals=(fundamentals or {}).get(symbol.upper()),
    )
    if candidates and cfg.check_term_structure:
        slope = term_structure_slope(provider, symbol, stats.spot, cfg, today)
        for candidate in candidates:
            candidate.term_slope = slope

    for candidate in candidates:
        score_candidate(candidate, cfg, context.regime_score)
    return candidates, stats, None


def term_structure_slope(
    provider: Provider,
    symbol: str,
    spot: float,
    cfg: WheelConfig,
    today: date,
) -> float:
    """Front ATM implied vol over a later expiry's, or NaN if unavailable.

    Deliberately called only for symbols that produced candidates. Most
    symbols scanned produce none, so paying for a second chain request on
    every one of them would roughly double the run for information about
    contracts already rejected.

    A failure here returns NaN rather than raising: an unavailable back month
    should cost the candidate its term-structure signal, not its place in the
    scan.
    """
    try:
        front = provider.option_chain(
            symbol,
            expiry_from=today + timedelta(days=cfg.min_dte),
            expiry_to=today + timedelta(days=cfg.max_dte),
            strike_min=spot * 0.97, strike_max=spot * 1.03, right="P",
        )
        back = provider.option_chain(
            symbol,
            expiry_from=today + timedelta(days=cfg.term_back_dte),
            expiry_to=today + timedelta(days=cfg.term_back_dte + 30),
            strike_min=spot * 0.97, strike_max=spot * 1.03, right="P",
        )
    except FetchError:
        return float("nan")

    front_atm = atm_iv(front, spot, today, cfg.risk_free_rate)
    back_atm = atm_iv(back, spot, today, cfg.risk_free_rate)
    if not front_atm or not back_atm:
        return float("nan")

    # Nearest expiry on the front, furthest on the back: the widest span the
    # two windows offer, which is where the signal is clearest.
    return term_slope(front_atm[min(front_atm)], back_atm[max(back_atm)])


def run_scan(
    provider: Provider,
    symbols: list[str],
    cfg: WheelConfig,
    earnings: EarningsCalendar,
    *,
    right: str = "P",
    positions: dict[str, tuple[float, float]] | None = None,
    fundamentals: dict[str, dict[str, float | None]] | None = None,
    verbose: bool = True,
) -> tuple[list[Candidate], Counter[str], dict[str, str], ScanContext]:
    """Scan the whole universe and return ranked candidates plus diagnostics."""
    context = prepare_context(provider, earnings)
    positions = positions or {}

    rejects: Counter[str] = Counter()
    skipped: dict[str, str] = {}
    everything: list[Candidate] = []

    for index, symbol in enumerate(symbols, 1):
        shares, basis = positions.get(symbol, (0.0, 0.0))
        if right == "C" and shares < 100:
            skipped[symbol] = "no covered shares"
            continue

        if verbose:
            print(f"[{index}/{len(symbols)}] {symbol}", end="", flush=True)

        try:
            found, _stats, reason = scan_symbol(
                provider,
                symbol,
                cfg,
                context,
                earnings,
                right=right,
                shares_held=shares,
                cost_basis=basis,
                rejects=rejects,
                fundamentals=fundamentals,
            )
        except Exception as exc:  # keep one bad symbol from ending the scan
            skipped[symbol] = f"error: {exc}"
            if verbose:
                print(f"  -> error: {exc}", file=sys.stderr)
            continue

        if reason:
            skipped[symbol] = reason
        everything.extend(found)
        if verbose:
            print(f"  -> {len(found)} candidate(s)" + (f", {reason}" if reason else ""))

    return rank(everything, cfg), rejects, skipped, context

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
    return low * (1 - STRIKE_PAD), high * (1 + STRIKE_PAD)


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
    for candidate in candidates:
        score_candidate(candidate, cfg, context.regime_score)
    return candidates, stats, None


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

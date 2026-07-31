"""Terminal and CSV output."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .analytics import UnderlyingStats
from .engine import ScanContext
from .orders import build_limit_plan, build_management_plan
from .strategy import Candidate

RIGHT_LABEL = {"P": "PUT", "C": "CALL"}


def fmt_num(value: float | None, spec: str, dash: str = "-") -> str:
    if value is None or value != value:
        return dash
    return format(value, spec)


def render_table(rows: list[list[str]], headers: list[str]) -> str:
    """Fixed-width table. Numeric-looking columns right-align."""
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def align(cell: str, width: int, index: int) -> str:
        return cell.ljust(width) if index <= 1 else cell.rjust(width)

    lines = ["  ".join(h.ljust(w) if i <= 1 else h.rjust(w)
                       for i, (h, w) in enumerate(zip(headers, widths)))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(align(c, w, i) for i, (c, w) in enumerate(zip(row, widths))))
    return "\n".join(lines)


def print_scan_table(candidates: list[Candidate]) -> None:
    if not candidates:
        print("\nNo candidate met every filter. Standards were not relaxed.")
        return

    headers = [
        "#", "Symbol", "Score", "Exp", "DTE", "Strike", "Bid/Ask", "Delta",
        "IV", "VRP", "Qty", "Credit", "Capital", "Ann.", "P(profit)", "Cushion",
    ]
    rows = []
    for i, c in enumerate(candidates, 1):
        rows.append([
            str(i),
            f"{c.symbol} {RIGHT_LABEL[c.right]}",
            f"{c.score:.1f}",
            c.expiration.strftime("%b %d"),
            str(c.dte),
            f"${c.strike:g}",
            f"{c.bid:.2f}/{c.ask:.2f}",
            f"{c.delta:+.2f}",
            fmt_num(c.iv, ".0%"),
            fmt_num(c.vrp, ".2f"),
            str(c.contracts),
            f"${c.credit:,.0f}",
            f"${c.capital:,.0f}",
            fmt_num(c.annualised_return, ".0%"),
            fmt_num(c.prob_profit, ".0%"),
            fmt_num(c.cushion_sigmas, ".2f") + "σ",
        ])
    print("\n" + render_table(rows, headers))


def print_trade_card(candidate: Candidate, *, index: int | None = None) -> None:
    """Full detail for one contract: what to sell, at what price, and the exit."""
    limits = build_limit_plan(candidate)
    plan = build_management_plan(candidate)
    label = RIGHT_LABEL[candidate.right]
    heading = f"{candidate.symbol} ${candidate.strike:g} {label}"
    prefix = f"#{index} " if index else ""

    print(f"\n{'=' * 74}")
    print(f"{prefix}{heading}  ·  score {candidate.score:.1f}/100")
    print("=" * 74)

    action = "SELL TO OPEN" if candidate.right == "P" else "SELL TO OPEN (covered)"
    print(f"  ORDER      {action} {candidate.contracts} × {heading}")
    print(f"             expiring {candidate.expiration:%A, %B %d %Y} ({candidate.dte} DTE)")
    print(f"             {candidate.occ_symbol}")
    print()
    print(f"  PRICE      Open at  ${limits.open_at:.2f}  limit credit  "
          f"→ ${limits.credit_open:,.0f} total")
    print(f"             Likely   ${limits.likely_fill:.2f}  (the mid)      "
          f"→ ${limits.credit_likely:,.0f} total")
    print(f"             Floor    ${limits.walk_floor:.2f}  do not go below "
          f"→ ${limits.credit_floor:,.0f} total")
    print(f"             Market is bid ${candidate.bid:.2f} / ask ${candidate.ask:.2f} "
          f"({candidate.spread_pct:.1%} wide, quote {candidate.quote_age_note})")
    print()
    print(f"  CAPITAL    ${candidate.capital:,.0f} "
          f"({'cash secured' if candidate.right == 'P' else 'shares held'})")
    print(f"  BREAKEVEN  ${candidate.breakeven:.2f} "
          f"({candidate.cushion_pct:+.1%} from ${candidate.spot:.2f} spot, "
          f"{fmt_num(candidate.cushion_sigmas, '.2f')}σ of the expected move)")
    print(f"  RETURN     {candidate.return_on_capital:.2%} over {candidate.dte} days "
          f"= {fmt_num(candidate.annualised_return, '.1%')} annualised")
    print(f"  ODDS       {fmt_num(candidate.prob_profit, '.0%')} chance of profit · "
          f"{fmt_num(candidate.prob_itm, '.0%')} chance of assignment · "
          f"delta {candidate.delta:+.3f}")
    print(f"  VOL        IV {fmt_num(candidate.iv, '.1%')} vs realised "
          f"{fmt_num(candidate.rv20, '.1%')} → VRP {fmt_num(candidate.vrp, '.2f')}"
          f"  ({_vrp_verdict(candidate.vrp)})")
    print(f"  DECAY      ${candidate.theta_per_day:+,.2f} per day from theta")
    print()
    print(f"  EXIT       Buy to close at ${plan.buyback_price:.2f} "
          f"({plan.profit_target_pct:.0%} of max profit, "
          f"${plan.profit_at_target:,.0f} locked in)")
    print(f"             Roll or close by {plan.roll_date:%b %d} "
          f"({plan.roll_dte} DTE) rather than holding into expiry")
    print(f"             {plan.stop_note}")
    print(f"  IF ASSIGNED {plan.assignment_note}")

    if candidate.earnings_date:
        print(f"  ⚠ EARNINGS  {candidate.earnings_date:%b %d} — after this expiry, "
              "but confirm the date before selling")

    # An earnings feed only covers scheduled reports. Implied volatility this
    # far above normal usually means the market has priced some catalyst the
    # calendar does not list: a court date, an FDA decision, a deal vote.
    if candidate.iv == candidate.iv and candidate.iv > 0.80:
        print(f"  ⚠ HIGH IV   {candidate.iv:.0%} implied is unusually rich. Check for a "
              "catalyst\n              (litigation, FDA, M&A, guidance) before assuming "
              "this is free premium.")


def _vrp_verdict(vrp: float) -> str:
    if vrp != vrp:
        return "realised vol unavailable"
    if vrp < 1.0:
        return "options are cheap — poor sale"
    if vrp < 1.15:
        return "thin edge"
    if vrp < 1.4:
        return "solid premium"
    if vrp < 2.5:
        return "rich premium"
    return "very rich — check for a pending event"


def print_context(context: ScanContext, cfg_note: str = "") -> None:
    session = "OPEN" if context.market_open else "CLOSED (quotes are last close)"
    print(f"Market session: {session}")
    print(f"Market regime:  {context.regime_score:.0f}/100 — {context.regime_note}")
    if not context.earnings_available:
        print("⚠ Earnings calendar unavailable; only earnings.csv overrides applied.")
    if cfg_note:
        print(cfg_note)


def print_diagnostics(rejects: Counter[str], skipped: dict[str, str]) -> None:
    print("\nWhy contracts were rejected:")
    accepted = rejects.pop("accepted", 0)
    for reason, count in rejects.most_common():
        print(f"  {count:>6}  {reason}")
    print(f"  {accepted:>6}  accepted")

    if skipped:
        print("\nSymbols skipped entirely:")
        grouped: dict[str, list[str]] = {}
        for symbol, reason in skipped.items():
            grouped.setdefault(reason, []).append(symbol)
        for reason, symbols in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            print(f"  {reason}: {', '.join(sorted(symbols))}")


def print_underlying_summary(symbol: str, stats: UnderlyingStats,
                             earnings_date=None) -> None:
    print(f"\n{symbol} — ${stats.spot:,.2f}")
    print(f"  Trend        {stats.trend_score:.0f}/100 · "
          f"20d SMA ${fmt_num(stats.sma20, ',.2f')} · "
          f"50d ${fmt_num(stats.sma50, ',.2f')} · "
          f"200d ${fmt_num(stats.sma200, ',.2f')}")
    print(f"  Moves        5d {fmt_num(stats.move_5d, '+.1%')} · "
          f"20d {fmt_num(stats.move_20d, '+.1%')} · "
          f"60d drawdown {fmt_num(stats.max_drawdown_60d, '+.1%')}")
    print(f"  Realised vol {fmt_num(stats.rv20, '.1%')} (20d) · "
          f"{fmt_num(stats.rv60, '.1%')} (60d) · "
          f"{fmt_num(stats.rv_percentile, '.0f')}th percentile of the last year")
    print(f"  Structure    20d support ${fmt_num(stats.support_20d, ',.2f')} · "
          f"20d resistance ${fmt_num(stats.resistance_20d, ',.2f')} · "
          f"ATR {fmt_num(stats.atr14_pct, '.1%')}")
    print(f"  Liquidity    ${stats.avg_dollar_volume / 1e6:,.0f}M traded per day")
    if earnings_date:
        print(f"  ⚠ Earnings   {earnings_date:%B %d, %Y}")


def write_csv(path: Path, candidates: list[Candidate]) -> None:
    if not candidates:
        path.write_text("", encoding="utf-8")
        return
    rows = []
    for candidate in candidates:
        row = asdict(candidate)
        subscores = row.pop("subscores", {}) or {}
        row.update({f"score_{k}": round(v, 1) for k, v in subscores.items()})
        limits = build_limit_plan(candidate)
        row.update(
            limit_open=limits.open_at,
            limit_likely=limits.likely_fill,
            limit_floor=limits.walk_floor,
        )
        rows.append(row)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

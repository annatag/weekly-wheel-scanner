#!/usr/bin/env python3
"""Offline tests for the pricing, sizing and order maths.

No network access: everything here is deterministic. Run with

    python -m unittest test_wheelkit -v
"""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import date

from wheelkit.analytics import (
    FALLING_KNIFE,
    MOMENTUM,
    PULLBACK,
    REBOUND,
    UNKNOWN,
    _trend_score,
    classify_setup,
    setup_score,
    compute_stats,
    variance_risk_premium,
)
from wheelkit.finviz import ScreenRow, _ScreenerParser, parse_filters
from wheelkit.orders import build_limit_plan, build_management_plan, round_to_tick, tick_size
from wheelkit.pricing import (
    black_scholes_price,
    compute_greeks,
    implied_vol,
    strike_for_delta,
)
from wheelkit.providers import Bar, Quote, parse_occ
from wheelkit.strategy import (
    Candidate,
    WheelConfig,
    build_candidates,
    fundamentals_pass,
    rank,
    resize_candidate,
    score_candidate,
    size_position,
    underlying_passes,
)

R = 0.04


class TestPricing(unittest.TestCase):
    def test_put_call_parity(self):
        spot, strike, t, vol = 100.0, 95.0, 14 / 365, 0.30
        call = black_scholes_price(spot, strike, t, vol, R, right="C")
        put = black_scholes_price(spot, strike, t, vol, R, right="P")
        self.assertAlmostEqual(call - put, spot - strike * math.exp(-R * t), places=9)

    def test_implied_vol_roundtrip(self):
        for vol in (0.12, 0.30, 0.85, 1.60):
            for strike in (80.0, 100.0, 120.0):
                price = black_scholes_price(100.0, strike, 21 / 365, vol, R, right="P")
                recovered = implied_vol(price, 100.0, strike, 21 / 365, R, right="P")
                if price < 0.005:
                    # Worth less than half a tick, so vol is unidentifiable.
                    self.assertIsNone(recovered, f"vol={vol} strike={strike}")
                    continue
                self.assertIsNotNone(recovered, f"vol={vol} strike={strike}")
                self.assertAlmostEqual(recovered, vol, places=4)

    def test_implied_vol_none_for_a_worthless_quote(self):
        # A far out-of-the-money put priced at a tick implies nothing usable.
        self.assertIsNone(implied_vol(0.001, 100.0, 60.0, 7 / 365, R, right="P"))

    def test_implied_vol_rejects_arbitrage_violating_price(self):
        # A put quoted below its discounted intrinsic implies no real vol.
        self.assertIsNone(implied_vol(0.001, 100.0, 130.0, 7 / 365, R, right="P"))

    def test_delta_matches_finite_difference(self):
        spot, strike, t, vol = 100.0, 92.0, 10 / 365, 0.45
        greeks = compute_greeks(spot, strike, t, vol, 0.5, R, right="P")
        eps = 1e-4
        numeric = (
            black_scholes_price(spot + eps, strike, t, vol, R, right="P")
            - black_scholes_price(spot - eps, strike, t, vol, R, right="P")
        ) / (2 * eps)
        self.assertAlmostEqual(greeks.delta, numeric, places=6)

    def test_theta_matches_one_day_decay(self):
        spot, strike, t, vol = 100.0, 90.0, 30 / 365, 0.35
        greeks = compute_greeks(spot, strike, t, vol, 0.5, R, right="P")
        decay = black_scholes_price(
            spot, strike, t - 1 / 365, vol, R, right="P"
        ) - black_scholes_price(spot, strike, t, vol, R, right="P")
        # Theta is instantaneous, so it only approximates a whole day.
        self.assertAlmostEqual(greeks.theta_per_day, decay, places=3)

    def test_strike_for_delta_inverts(self):
        for target in (0.10, 0.20, 0.35):
            for right in ("P", "C"):
                strike = strike_for_delta(100.0, target, 14 / 365, 0.40, R, right=right)
                greeks = compute_greeks(100.0, strike, 14 / 365, 0.40, 0.5, R, right=right)
                self.assertAlmostEqual(abs(greeks.delta), target, places=4)

    def test_probabilities_are_coherent(self):
        greeks = compute_greeks(100.0, 90.0, 14 / 365, 0.35, 0.60, R, right="P")
        # Breakeven sits below the strike, so profit is likelier than staying OTM.
        self.assertGreater(greeks.prob_profit, 1 - greeks.prob_itm)
        self.assertTrue(0 < greeks.prob_itm < 1)
        self.assertTrue(0 < greeks.prob_profit < 1)


class TestSizing(unittest.TestCase):
    def setUp(self):
        self.cfg = WheelConfig(min_cash=3_000, max_cash=15_000)

    def test_cheap_stock_uses_multiple_contracts(self):
        # The previous scanner derived a $70 minimum strike from the cash floor
        # and silently dropped every stock below it.
        self.assertEqual(size_position(15.0, self.cfg), (10, 15_000.0))
        self.assertEqual(size_position(8.0, self.cfg), (18, 14_400.0))

    def test_expensive_stock_rejected_unless_oversize_allowed(self):
        self.assertIsNone(size_position(300.0, self.cfg))
        permissive = WheelConfig(min_cash=0, max_cash=15_000, allow_single_oversize=True)
        self.assertEqual(size_position(300.0, permissive), (1, 30_000.0))

    def test_position_below_cash_floor_rejected(self):
        # A $29 strike allows five contracts ($14,500); one alone is too small.
        self.assertIsNone(size_position(29.0, WheelConfig(min_cash=3_000, max_cash=2_900)))


class TestOrders(unittest.TestCase):
    def test_tick_size_boundary(self):
        self.assertEqual(tick_size(2.99), 0.01)
        self.assertEqual(tick_size(3.00), 0.05)

    def test_round_to_tick(self):
        self.assertAlmostEqual(round_to_tick(1.234), 1.23)
        self.assertAlmostEqual(round_to_tick(4.27, "down"), 4.25)

    def test_limit_ladder_is_ordered_and_within_the_spread(self):
        candidate = _candidate(bid=1.00, ask=1.40, mid=1.20)
        plan = build_limit_plan(candidate)
        self.assertGreaterEqual(plan.open_at, plan.likely_fill)
        self.assertGreaterEqual(plan.likely_fill, plan.walk_floor)
        self.assertLessEqual(plan.open_at, candidate.ask)
        self.assertGreaterEqual(plan.walk_floor, candidate.bid)

    def test_limit_ladder_survives_a_one_tick_spread(self):
        candidate = _candidate(bid=0.30, ask=0.31, mid=0.305)
        plan = build_limit_plan(candidate)
        self.assertGreaterEqual(plan.open_at, plan.likely_fill)
        self.assertGreaterEqual(plan.likely_fill, plan.walk_floor)

    def test_management_plan_targets_half_the_credit(self):
        candidate = _candidate(bid=2.00, ask=2.10, mid=2.05)
        plan = build_management_plan(candidate)
        self.assertAlmostEqual(plan.buyback_price, round_to_tick(1.025))
        self.assertLess(plan.roll_date, candidate.expiration)


class TestProviders(unittest.TestCase):
    def test_parse_occ(self):
        self.assertEqual(
            parse_occ("AAPL260807P00290000"),
            ("AAPL", date(2026, 8, 7), "P", 290.0),
        )
        self.assertEqual(
            parse_occ("SOFI260821C00015500"),
            ("SOFI", date(2026, 8, 21), "C", 15.5),
        )
        self.assertIsNone(parse_occ("not-an-occ-symbol"))


class TestAnalytics(unittest.TestCase):
    def test_variance_risk_premium(self):
        self.assertAlmostEqual(variance_risk_premium(0.40, 0.20, 0.20), 2.0)
        self.assertTrue(math.isnan(variance_risk_premium(0.40, float("nan"), float("nan"))))

    def test_compute_stats_on_a_synthetic_uptrend(self):
        bars = [
            Bar(date(2026, 1, 1), 100 + i, 101 + i, 99 + i, 100.5 + i, 5_000_000)
            for i in range(120)
        ]
        stats = compute_stats(bars, 220.0)
        self.assertIsNotNone(stats)
        self.assertGreater(stats.trend_score, 50)  # rising series
        self.assertGreater(stats.avg_dollar_volume, 0)

    def test_compute_stats_needs_history(self):
        self.assertIsNone(compute_stats([], 100.0))


class TestStrategy(unittest.TestCase):
    def test_score_rewards_premium_above_the_risk_free_rate(self):
        cfg = WheelConfig()
        weak = _candidate(annualised=0.05)  # barely above cash
        strong = _candidate(annualised=0.45)
        score_candidate(weak, cfg, 50.0)
        score_candidate(strong, cfg, 50.0)
        self.assertLess(weak.subscores["premium"], 5)
        self.assertGreater(strong.subscores["premium"], 50)

    def test_score_penalises_selling_cheap_volatility(self):
        cfg = WheelConfig()
        cheap = _candidate(vrp=0.90)
        rich = _candidate(vrp=1.60)
        score_candidate(cheap, cfg, 50.0)
        score_candidate(rich, cfg, 50.0)
        self.assertLess(cheap.subscores["iv_edge"], 15)
        self.assertGreater(rich.subscores["iv_edge"], 85)

    def test_rank_enforces_one_contract_per_symbol(self):
        cfg = WheelConfig(top_n=3, one_per_symbol=True)
        pool = [_candidate(symbol="AAA", score=s) for s in (90, 88, 86)]
        pool += [_candidate(symbol="BBB", score=80), _candidate(symbol="CCC", score=70)]
        picked = rank(pool, cfg)
        self.assertEqual([c.symbol for c in picked], ["AAA", "BBB", "CCC"])

    def test_rank_can_allow_duplicates(self):
        cfg = WheelConfig(top_n=3, one_per_symbol=False)
        pool = [_candidate(symbol="AAA", score=s) for s in (90, 88, 86)]
        self.assertEqual(len(rank(pool, cfg)), 3)

    def test_resize_preserves_per_contract_economics(self):
        candidate = _candidate(bid=1.00, ask=1.10, mid=1.05)
        before = candidate.return_on_capital
        resize_candidate(candidate, 4)
        self.assertEqual(candidate.contracts, 4)
        self.assertAlmostEqual(candidate.credit, 1.05 * 100 * 4)
        # Return on capital is scale-invariant for a cash-secured put.
        self.assertAlmostEqual(candidate.return_on_capital, before, places=9)

    def test_earnings_inside_the_expiry_is_excluded(self):
        cfg = WheelConfig(min_dte=1, max_dte=60, min_avg_dollar_volume=0,
                          min_option_volume=0, min_quote_size=0, min_vrp=0.0,
                          min_annualised_return=0.0)
        stats = compute_stats(
            [Bar(date(2026, 1, 1), 100, 101, 99, 100, 9_000_000) for _ in range(120)],
            100.0,
        )
        quote = Quote(
            occ_symbol="TEST260821P00090000", underlying="TEST",
            expiration=date(2026, 8, 21), strike=90.0, right="P",
            bid=1.00, ask=1.06, bid_size=10, ask_size=10, volume=100,
            open_interest=None, quote_time=None,
        )
        common = dict(right="P", today=date(2026, 8, 1), market_open=False)
        without = build_candidates("TEST", [quote], stats, cfg, **common)
        with_event = build_candidates(
            "TEST", [quote], stats, cfg, earnings_date=date(2026, 8, 10), **common
        )
        self.assertEqual(len(without), 1)
        self.assertEqual(len(with_event), 0)


class TestSetupClassification(unittest.TestCase):
    def test_two_by_two(self):
        self.assertEqual(classify_setup(0.15, -0.06), PULLBACK)
        self.assertEqual(classify_setup(0.15, 0.06), MOMENTUM)
        self.assertEqual(classify_setup(-0.15, 0.06), REBOUND)
        self.assertEqual(classify_setup(-0.15, -0.06), FALLING_KNIFE)

    def test_missing_history_is_unknown(self):
        nan = float("nan")
        self.assertEqual(classify_setup(nan, -0.06), UNKNOWN)
        self.assertEqual(classify_setup(0.15, nan), UNKNOWN)

    def test_setup_ranks_pullback_highest_and_knife_lowest(self):
        # The previous model subtracted move_20d * 100, so the dip alone made
        # a pullback score below an otherwise identical momentum name.
        self.assertGreater(setup_score(PULLBACK), setup_score(MOMENTUM))
        self.assertGreater(setup_score(MOMENTUM), setup_score(REBOUND))
        self.assertGreater(setup_score(REBOUND), setup_score(FALLING_KNIFE))
        self.assertEqual(setup_score(UNKNOWN), 50.0)

    def test_trend_score_measures_structure_only(self):
        # Setup carries its own weight now, so trend must not double-count it.
        strong = _trend_score(spot=110.0, sma20=105.0, sma50=100.0, sma200=90.0)
        weak = _trend_score(spot=80.0, sma20=105.0, sma50=100.0, sma200=90.0)
        self.assertGreater(strong, weak)

    def test_setup_actually_moves_the_final_score(self):
        # Buried inside safety this whole range moved the total under 2 points.
        cfg = WheelConfig()
        pull = _candidate(setup=PULLBACK)
        knife = _candidate(setup=FALLING_KNIFE)
        score_candidate(pull, cfg, 50.0)
        score_candidate(knife, cfg, 50.0)
        self.assertGreater(pull.score - knife.score, 10.0)

    def test_quarter_move_uses_63_sessions(self):
        bars = [
            Bar(date(2026, 1, 1), 100 + i, 101 + i, 99 + i, 100 + i, 5_000_000)
            for i in range(120)
        ]
        stats = compute_stats(bars, 220.0)
        self.assertEqual(stats.setup, MOMENTUM)  # rising on both horizons
        self.assertAlmostEqual(stats.move_quarter, 220.0 / bars[-64].close - 1, places=9)


class TestFundamentalsGate(unittest.TestCase):
    DATA = {
        "AIG": {"pe": 13.93, "peg": 0.85},
        "VRSK": {"pe": 28.57, "peg": 1.97},
        "RICH": {"pe": 45.0, "peg": 1.0},
        "GROWTHY": {"pe": 20.0, "peg": 3.5},
        "ETF": {"pe": None, "peg": None},
        "LOSSMAKER": {"pe": -5.0, "peg": None},
    }

    def test_gate_is_off_unless_configured(self):
        self.assertIsNone(fundamentals_pass("RICH", self.DATA, WheelConfig()))

    def test_rejects_on_pe_and_peg(self):
        cfg = WheelConfig(max_pe=30, max_peg=2.0)
        self.assertIsNone(fundamentals_pass("AIG", self.DATA, cfg))
        self.assertIsNone(fundamentals_pass("VRSK", self.DATA, cfg))
        self.assertIsNotNone(fundamentals_pass("RICH", self.DATA, cfg))
        self.assertIsNotNone(fundamentals_pass("GROWTHY", self.DATA, cfg))

    def test_unprofitable_is_always_rejected(self):
        cfg = WheelConfig(max_pe=30)
        self.assertIn("unprofitable", fundamentals_pass("LOSSMAKER", self.DATA, cfg))

    def test_etfs_pass_unless_fundamentals_required(self):
        lenient = WheelConfig(max_pe=30, max_peg=2.0)
        strict = WheelConfig(max_pe=30, max_peg=2.0, require_fundamentals=True)
        self.assertIsNone(fundamentals_pass("ETF", self.DATA, lenient))
        self.assertIsNotNone(fundamentals_pass("ETF", self.DATA, strict))
        self.assertIsNotNone(fundamentals_pass("NOTLISTED", self.DATA, strict))


class TestFinvizParsing(unittest.TestCase):
    HTML = """
    <table><thead>
      <th class="table-header">No.</th><th class="table-header">Ticker</th>
      <th class="table-header">Market Cap</th><th class="table-header">P/E</th>
      <th class="table-header">PEG</th><th class="table-header">Price</th>
    </thead>
    <tr class="styled-row is-striped" valign="top">
      <td>1</td>
      <td data-boxover-ticker="AIG" data-boxover-company="American Intl"><a>AAIG</a></td>
      <td>39.76B</td><td>13.93</td><td>0.85</td><td>76.03</td>
    </tr>
    <tr class="styled-row is-striped" valign="top">
      <td>2</td>
      <td data-boxover-ticker="GS" data-boxover-company="Goldman"><a>GGS</a></td>
      <td>303.58B</td><td>16.09</td><td>-</td><td>1042.63</td>
    </tr>
    </table>"""

    def _rows(self):
        parser = _ScreenerParser()
        parser.feed(self.HTML)
        return parser

    def test_ticker_comes_from_the_attribute_not_the_cell_text(self):
        # The logo's alt text runs into the cell, rendering "AAIG" for AIG.
        parser = self._rows()
        self.assertEqual([t for t, _ in parser.rows], ["AIG", "GS"])

    def test_rows_align_with_their_own_tickers(self):
        parser = self._rows()
        headers = parser.headers
        rows = {t: ScreenRow(t, dict(zip(headers, cells))) for t, cells in parser.rows}
        self.assertAlmostEqual(rows["AIG"].number("P/E"), 13.93)
        self.assertAlmostEqual(rows["AIG"].number("Price"), 76.03)
        self.assertAlmostEqual(rows["GS"].number("P/E"), 16.09)
        self.assertAlmostEqual(rows["GS"].number("Price"), 1042.63)

    def test_number_parsing(self):
        row = ScreenRow("X", {"cap": "39.76B", "vol": "2,132,694", "chg": "2.83%",
                              "missing": "-", "na": "N/A"})
        self.assertAlmostEqual(row.number("cap"), 39.76e9)
        self.assertAlmostEqual(row.number("vol"), 2132694)
        self.assertAlmostEqual(row.number("chg"), 2.83)
        self.assertIsNone(row.number("missing"))
        self.assertIsNone(row.number("na"))
        self.assertIsNone(row.number("absent"))

    def test_parse_filters(self):
        url = ("https://finviz.com/screener.ashx?v=121&f=fa_pe_u30,fa_peg_u2,"
               "ta_perf_13wup&o=ticker")
        self.assertEqual(parse_filters(url), "fa_pe_u30,fa_peg_u2,ta_perf_13wup")
        with self.assertRaises(ValueError):
            parse_filters("https://finviz.com/screener.ashx?v=121")


class TestTrendGate(unittest.TestCase):
    def _stats(self, quarter: float, month: float):
        bars = [Bar(date(2026, 1, 1), 100, 101, 99, 100, 9_000_000) for _ in range(120)]
        stats = compute_stats(bars, 100.0)
        return replace(stats, move_quarter=quarter, move_20d=month,
                       setup=classify_setup(quarter, month))

    def test_falling_knife_excluded_by_default(self):
        cfg = WheelConfig(min_avg_dollar_volume=0)
        self.assertIsNotNone(underlying_passes(self._stats(-0.2, -0.1), cfg, "P"))
        self.assertIsNone(underlying_passes(self._stats(0.2, -0.05), cfg, "P"))

    def test_require_pullback_rejects_momentum(self):
        cfg = WheelConfig(min_avg_dollar_volume=0, require_pullback=True)
        self.assertIsNone(underlying_passes(self._stats(0.2, -0.05), cfg, "P"))
        self.assertIsNotNone(underlying_passes(self._stats(0.2, 0.05), cfg, "P"))

    def test_trend_gates_do_not_apply_to_covered_calls(self):
        # A covered call is written against shares already held; the entry
        # decision was made long ago.
        cfg = WheelConfig(min_avg_dollar_volume=0, require_pullback=True)
        self.assertIsNone(underlying_passes(self._stats(-0.2, -0.1), cfg, "C"))


def _candidate(**overrides) -> Candidate:
    """A filled-in candidate for tests that only care about a few fields."""
    defaults = dict(
        symbol="TEST", right="P", occ_symbol="TEST260807P00090000",
        expiration=date(2026, 8, 7), dte=7, strike=90.0, spot=100.0,
        bid=1.00, ask=1.10, mid=1.05, spread_pct=0.095,
        option_volume=250, open_interest=None,
        iv=0.35, delta=-0.20, theta_per_day=-12.0,
        prob_itm=0.20, prob_profit=0.82, vrp=1.30,
        contracts=1, capital=9_000.0, credit=105.0, breakeven=88.95,
        cushion_pct=0.11, cushion_sigmas=0.90,
        return_on_capital=105.0 / 9_000.0, annualised_return=0.61,
        trend_score=60.0, avg_dollar_volume=250_000_000.0, rv20=0.27,
        move_5d=0.01, support_20d=86.0, earnings_date=None,
        quote_age_note="fresh",
    )
    score = overrides.pop("score", None)
    annualised = overrides.pop("annualised", None)
    if annualised is not None:
        overrides["annualised_return"] = annualised
    defaults.update(overrides)
    candidate = Candidate(**defaults)
    if score is not None:
        candidate.score = score
    return candidate


if __name__ == "__main__":
    unittest.main()

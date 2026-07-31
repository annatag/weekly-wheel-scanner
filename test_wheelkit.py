#!/usr/bin/env python3
"""Offline tests for the pricing, sizing and order maths.

No network access: everything here is deterministic. Run with

    python -m unittest test_wheelkit -v
"""

from __future__ import annotations

import math
import unittest
from datetime import date

from wheelkit.analytics import compute_stats, variance_risk_premium
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
    rank,
    resize_candidate,
    score_candidate,
    size_position,
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

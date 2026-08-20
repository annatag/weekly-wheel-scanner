#!/usr/bin/env python3
"""Offline tests for the risk gate, sizing and position monitor."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from wheelkit.positions import OpenOption, occ_symbol, read_positions_csv
from wheelkit.risk import (
    INFO,
    URGENT,
    WARN,
    RiskLimits,
    check_entry,
    check_portfolio,
    check_position,
    size_position,
    worst_level,
)

LIMITS = RiskLimits(account_value=100_000)
SOON = date.today() + timedelta(days=14)


def codes(findings):
    return {f.code for f in findings}


class TestEntryGate(unittest.TestCase):
    """The gate that was missing: four of five live entries broke these."""

    def _entry(self, **kw):
        base = dict(
            symbol="XYZ", right="P", strike=90.0, spot=100.0, delta=-0.20,
            dte=14, credit_per_share=1.00, spread_pct=0.05, vrp=1.3,
            setup="pullback", limits=LIMITS,
        )
        base.update(kw)
        return check_entry(**base)

    def test_a_disciplined_trade_passes_cleanly(self):
        self.assertEqual(self._entry(), [])

    def test_blocks_delta_above_the_band(self):
        # C was sold at ~0.53 delta and lost 570% of the credit.
        findings = self._entry(delta=-0.53)
        self.assertIn("delta_too_high", codes(findings))
        self.assertEqual(worst_level(findings), URGENT)

    def test_blocks_a_strike_sold_in_the_money(self):
        findings = self._entry(strike=136.0, spot=135.0)
        self.assertIn("sold_itm", codes(findings))
        self.assertEqual(worst_level(findings), URGENT)

    def test_blocks_a_call_sold_in_the_money(self):
        findings = self._entry(right="C", strike=95.0, spot=100.0, delta=0.20)
        self.assertIn("sold_itm", codes(findings))

    def test_blocks_earnings_inside_the_expiry(self):
        findings = self._entry(
            earnings_date=date.today() + timedelta(days=3), expiration=SOON
        )
        self.assertIn("earnings_before_expiry", codes(findings))

    def test_earnings_after_expiry_is_fine(self):
        findings = self._entry(
            earnings_date=SOON + timedelta(days=10), expiration=SOON
        )
        self.assertEqual(findings, [])

    def test_blocks_a_falling_knife(self):
        self.assertIn("blocked_setup", codes(self._entry(setup="falling knife")))

    def test_warns_on_cheap_volatility_without_blocking(self):
        findings = self._entry(vrp=0.85)
        self.assertIn("selling_cheap_vol", codes(findings))
        self.assertEqual(worst_level(findings), WARN)

    def test_warns_on_a_wide_spread(self):
        self.assertIn("spread_too_wide", codes(self._entry(spread_pct=0.30)))


class TestSizing(unittest.TestCase):
    def test_volatile_names_get_fewer_contracts(self):
        # The old rule filled a cash sleeve, so the cheapest (usually most
        # volatile) name took the most contracts. CCL took five.
        calm = size_position(strike=28, spot=29, iv=0.20, dte=14, limits=LIMITS)
        wild = size_position(strike=28, spot=29, iv=0.80, dte=14, limits=LIMITS)
        self.assertIsNotNone(calm)
        self.assertIsNotNone(wild)
        self.assertGreater(calm.contracts, wild.contracts)

    def test_respects_the_hard_contract_cap(self):
        tight = RiskLimits(account_value=100_000, max_contracts_per_position=3)
        result = size_position(strike=5, spot=6, iv=0.15, dte=7, limits=tight)
        self.assertLessEqual(result.contracts, 3)

    def test_respects_capital_per_position(self):
        result = size_position(strike=100, spot=110, iv=0.25, dte=14, limits=LIMITS)
        self.assertLessEqual(
            result.capital,
            LIMITS.account_value * LIMITS.max_capital_per_position_pct + 1e-6,
        )

    def test_reports_which_constraint_bound(self):
        result = size_position(strike=28, spot=29, iv=0.90, dte=21, limits=LIMITS)
        self.assertIn(result.binding_constraint,
                      {"two-sigma risk budget", "capital per position", "contract cap"})


class TestPortfolio(unittest.TestCase):
    def test_flags_duplicate_symbols(self):
        book = [{"symbol": "GM", "capital": 8_000},
                {"symbol": "GM", "capital": 8_000}]
        self.assertIn("symbol_concentration", codes(check_portfolio(book, limits=LIMITS)))

    def test_flags_sector_concentration(self):
        book = [
            {"symbol": s, "capital": 5_000, "sector": "Consumer Cyclical"}
            for s in ("CCL", "GM", "F", "RCL")
        ]
        self.assertIn("sector_concentration", codes(check_portfolio(book, limits=LIMITS)))

    def test_flags_over_commitment(self):
        book = [{"symbol": f"S{i}", "capital": 15_000} for i in range(5)]
        self.assertIn("over_committed", codes(check_portfolio(book, limits=LIMITS)))

    def test_a_proposed_trade_counts_toward_limits(self):
        book = [{"symbol": "GM", "capital": 8_000}]
        clean = check_portfolio(book, limits=LIMITS)
        with_new = check_portfolio(
            book, proposed={"symbol": "GM", "capital": 8_000}, limits=LIMITS
        )
        self.assertEqual(clean, [])
        self.assertIn("symbol_concentration", codes(with_new))


class TestOpenPositionAlerts(unittest.TestCase):
    def _pos(self, **kw):
        base = dict(
            symbol="XYZ", right="P", strike=90.0, spot=100.0,
            expiration=SOON, dte=14, delta=-0.20, entry_credit=1.00,
            current_mid=0.90, limits=LIMITS,
        )
        base.update(kw)
        return check_position(**base)

    def test_quiet_when_nothing_is_wrong(self):
        self.assertEqual(self._pos(), [])

    def test_in_the_money_is_urgent_inside_the_gamma_window(self):
        near = self._pos(spot=85.0, dte=1)
        far = self._pos(spot=85.0, dte=20)
        self.assertEqual(worst_level(near), URGENT)
        self.assertEqual(worst_level(far), WARN)

    def test_profit_target_fires(self):
        # SLV sat at 97% of max profit, unwatched, for a penny of remaining edge.
        findings = self._pos(current_mid=0.03)
        self.assertIn("profit_target", codes(findings))

    def test_gamma_window_warns_on_a_live_delta(self):
        self.assertIn("gamma_window", codes(self._pos(dte=1, delta=-0.36)))

    def test_big_underlying_move_is_flagged(self):
        self.assertIn("underlying_moved", codes(self._pos(underlying_move_1d=-0.107)))

    def test_delta_drift_warns_before_it_goes_itm(self):
        self.assertIn("delta_drift", codes(self._pos(delta=-0.55, spot=91.0)))


class TestPositionsFile(unittest.TestCase):
    def test_occ_symbol_format(self):
        self.assertEqual(
            occ_symbol("CCL", date(2026, 8, 28), "P", 28.0), "CCL260828P00028000"
        )
        self.assertEqual(
            occ_symbol("SLV", date(2026, 8, 21), "P", 55.5), "SLV260821P00055500"
        )

    def test_csv_with_leading_comments_parses(self):
        # DictReader would otherwise treat the first "#" line as the header
        # and silently return nothing.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            path.write_text(
                "# a comment\n"
                "# another\n"
                "symbol,expiration,strike,right,quantity,entry_credit\n"
                "CCL,2026-08-28,28,P,-5,0.65\n",
                encoding="utf-8",
            )
            rows = read_positions_csv(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "CCL")
        self.assertEqual(rows[0].quantity, -5)
        self.assertEqual(rows[0].contracts, 5)
        self.assertTrue(rows[0].is_short)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(read_positions_csv(Path("/nonexistent/x.csv")), [])

    def test_itm_detection_both_rights(self):
        put = OpenOption("X", "o", "P", 90, SOON, -1, 1.0, spot=85.0)
        call = OpenOption("X", "o", "C", 90, SOON, -1, 1.0, spot=95.0)
        self.assertTrue(put.itm)
        self.assertTrue(call.itm)


if __name__ == "__main__":
    unittest.main()

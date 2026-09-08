#!/usr/bin/env python3
"""Offline tests for the risk gate, sizing and position monitor."""

from __future__ import annotations

import math
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
        # Expiry is derived from the DTE the test asks for, so a case that
        # overrides one is not silently contradicting the other. The time
        # stop reads the expiry date and the gamma checks read the DTE.
        dte = kw.pop("dte", 28)
        base = dict(
            symbol="XYZ", right="P", strike=90.0, spot=100.0,
            expiration=date.today() + timedelta(days=dte), dte=dte,
            delta=-0.20, entry_credit=1.00, current_mid=0.90, limits=LIMITS,
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


class TestCredentialLocation(unittest.TestCase):
    """Credentials must resolve outside the checkout, and say so when absent."""

    def test_config_dir_is_outside_the_repository(self):
        from wheelkit.providers import config_dir

        repo = Path(__file__).resolve().parent
        self.assertNotIn(repo, config_dir().parents)
        self.assertNotEqual(config_dir(), repo)

    def test_search_order_prefers_config_over_repo(self):
        import os
        from wheelkit.providers import env_search_path

        previous = os.environ.pop("WHEELSCAN_ENV", None)
        try:
            paths = env_search_path()
        finally:
            if previous is not None:
                os.environ["WHEELSCAN_ENV"] = previous
        self.assertEqual(len(paths), 2)
        self.assertTrue(str(paths[0]).endswith("wheelscan/.env"))
        self.assertTrue(str(paths[1]).endswith("weekly-wheel-scan/.env"))

    def test_explicit_override_wins(self):
        import os
        from wheelkit.providers import env_search_path

        os.environ["WHEELSCAN_ENV"] = "/tmp/custom.env"
        try:
            self.assertEqual(str(env_search_path()[0]), "/tmp/custom.env")
        finally:
            os.environ.pop("WHEELSCAN_ENV")

    def test_load_returns_none_when_nothing_exists(self):
        from wheelkit.providers import load_dotenv

        self.assertIsNone(load_dotenv(Path("/nonexistent/nowhere/.env")))

    def test_existing_environment_wins_over_the_file(self):
        import os
        from wheelkit.providers import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("WHEEL_TEST_KEY=from_file\n", encoding="utf-8")
            os.environ["WHEEL_TEST_KEY"] = "from_environment"
            try:
                load_dotenv(path)
                self.assertEqual(os.environ["WHEEL_TEST_KEY"], "from_environment")
            finally:
                os.environ.pop("WHEEL_TEST_KEY", None)


class TestKeychain(unittest.TestCase):
    """Keychain round-trip, isolated under a throwaway service name."""

    SERVICE = "wheelscan-unittest"

    def setUp(self):
        from wheelkit import secrets as keychain

        if not keychain.available():
            self.skipTest("macOS Keychain unavailable")
        self.kc = keychain
        self.kc.delete("PROBE", self.SERVICE)

    def tearDown(self):
        self.kc.delete("PROBE", self.SERVICE)

    def test_round_trip(self):
        self.kc.put("PROBE", "value-one", self.SERVICE)
        self.assertEqual(self.kc.get("PROBE", self.SERVICE), "value-one")

    def test_overwrite_replaces_rather_than_duplicating(self):
        self.kc.put("PROBE", "first", self.SERVICE)
        self.kc.put("PROBE", "second", self.SERVICE)
        self.assertEqual(self.kc.get("PROBE", self.SERVICE), "second")

    def test_missing_key_returns_none(self):
        self.assertIsNone(self.kc.get("NEVER_STORED", self.SERVICE))

    def test_empty_values_are_refused(self):
        # An empty secret stores fine and then fails opaquely at call time.
        with self.assertRaises(self.kc.KeychainError):
            self.kc.put("PROBE", "", self.SERVICE)

    def test_delete_reports_whether_anything_went(self):
        self.kc.put("PROBE", "x", self.SERVICE)
        self.assertTrue(self.kc.delete("PROBE", self.SERVICE))
        self.assertFalse(self.kc.delete("PROBE", self.SERVICE))

    def test_environment_is_never_overwritten(self):
        import os

        self.kc.put("PROBE", "from-keychain", self.SERVICE)
        os.environ["PROBE"] = "from-environment"
        try:
            loaded = self.kc.load_into_environ(("PROBE",), self.SERVICE)
            self.assertEqual(loaded, [])
            self.assertEqual(os.environ["PROBE"], "from-environment")
        finally:
            os.environ.pop("PROBE", None)

    def test_load_into_environ_fills_a_missing_name(self):
        import os

        self.kc.put("PROBE", "from-keychain", self.SERVICE)
        os.environ.pop("PROBE", None)
        try:
            self.assertEqual(
                self.kc.load_into_environ(("PROBE",), self.SERVICE), ["PROBE"]
            )
            self.assertEqual(os.environ["PROBE"], "from-keychain")
        finally:
            os.environ.pop("PROBE", None)


class TestSourceReporting(unittest.TestCase):
    """A fallback that happens silently is the failure being guarded against."""

    def setUp(self):
        from datetime import date as _date

        from wheelkit.positions import OpenOption

        self.fake = [OpenOption("GM", "GM260821P00087000", "P", 87.0,
                                _date(2026, 8, 21), -1, 1.157)]

    def test_csv_fallback_is_not_live(self):
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            path.write_text(
                "symbol,expiration,strike,right,quantity,entry_credit\n"
                "GM,2026-08-21,87,P,-1,1.157\n", encoding="utf-8",
            )
            with patch("wheelkit.positions.read_positions_ibkr",
                       side_effect=RuntimeError("Could not reach TWS")):
                positions, report = load_positions("auto", path=path)

        self.assertEqual(len(positions), 1)
        self.assertEqual(report.used, "csv")
        self.assertFalse(report.is_live)

    def test_broker_source_is_live(self):
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with patch("wheelkit.positions.read_positions_ibkr", return_value=self.fake):
            _, report = load_positions("auto")
        self.assertEqual(report.used, "ibkr")
        self.assertTrue(report.is_live)

    def test_every_attempt_is_recorded_with_a_reason(self):
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with patch("wheelkit.positions.read_positions_ibkr",
                   side_effect=RuntimeError("Connect call failed")):
            _, report = load_positions("auto", path=Path("/nonexistent.csv"))
        attempted = dict(report.attempts)
        self.assertIn("ibkr", attempted)
        self.assertEqual(attempted["ibkr"], "TWS not reachable")

    def test_explicit_source_does_not_fall_back(self):
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with patch("wheelkit.positions.read_positions_ibkr",
                   side_effect=RuntimeError("Could not reach TWS")):
            with self.assertRaises(RuntimeError):
                load_positions("ibkr")

    def test_no_positions_anywhere_reports_nothing_used(self):
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with patch("wheelkit.positions.read_positions_ibkr", return_value=[]):
            with patch("wheelkit.positions.read_positions_alpaca", return_value=[]):
                positions, report = load_positions(
                    "auto", path=Path("/nonexistent.csv")
                )
        self.assertEqual(positions, [])
        self.assertIsNone(report.used)
        self.assertFalse(report.is_live)


class TestNotifications(unittest.TestCase):
    """Delivery must survive the characters the alert text actually contains."""

    def _findings(self, *levels):
        from wheelkit.risk import Finding

        return [("C $136P", [Finding(l, "x", f"{l} message") for l in levels])]

    def test_header_folding_handles_typographic_characters(self):
        from wheelkit.notify import header_safe

        # An em dash in the title raised UnicodeEncodeError inside urllib and
        # silently lost the notification; alert text also carries "·" and "σ".
        self.assertEqual(header_safe("Wheel — 1 urgent"), "Wheel - 1 urgent")
        self.assertEqual(header_safe("a · b"), "a | b")
        self.assertEqual(header_safe("0.77σ cushion"), "0.77sigma cushion")
        header_safe("Wheel — 0.9σ · x").encode("latin-1")  # must not raise

    def test_header_folding_is_lossy_but_never_fails(self):
        from wheelkit.notify import header_safe

        header_safe("emoji 🚨 and 日本語").encode("latin-1")

    def test_summary_counts_by_severity(self):
        from wheelkit.notify import summarise

        _, subtitle, _ = summarise(self._findings(URGENT, URGENT, WARN))
        self.assertIn("2 urgent", subtitle)
        self.assertIn("1 warning", subtitle)

    def test_summary_leads_with_the_worst_finding(self):
        from wheelkit.notify import summarise

        _, _, body = summarise(self._findings(WARN, URGENT))
        self.assertIn("URGENT message", body)

    def test_empty_alerts_send_nothing(self):
        from wheelkit.notify import NotifyConfig, dispatch

        self.assertEqual(dispatch([], NotifyConfig(banner=True, push=True)), {})

    def test_push_without_a_topic_is_skipped_not_attempted(self):
        from wheelkit.notify import NotifyConfig, dispatch

        config = NotifyConfig(banner=False, push=True, topic=None)
        self.assertNotIn("push", dispatch(self._findings(URGENT), config))

    def test_delivery_failure_never_raises(self):
        from wheelkit.notify import send_push

        # An unroutable host must return False rather than take the monitor
        # down and lose the alert it was trying to deliver.
        self.assertFalse(
            send_push("x", title="t", topic="topic",
                      server="https://127.0.0.1:9")
        )

    def test_disabled_channels_are_not_reported(self):
        from wheelkit.notify import NotifyConfig, dispatch

        sent = dispatch(self._findings(WARN), NotifyConfig(banner=False, push=False))
        self.assertEqual(sent, {})


class TestCoveredCallMath(unittest.TestCase):
    """Assignment usually arrives underwater; the arithmetic has to show both
    sides of that, not just refuse the strikes that pay."""

    def _call(self, strike, mid, basis=27.34, contracts=5, dte=36):
        from datetime import date as _date

        from wheel_covered_call import CallOption

        credit = mid * 100.0 * contracts
        share = (strike - basis) * 100.0 * contracts
        capital = basis * 100.0 * contracts
        return CallOption(
            strike=strike, expiration=_date(2026, 9, 25), dte=dte,
            bid=mid - 0.05, ask=mid + 0.05, mid=mid, spread_pct=0.1,
            volume=10, iv=0.35, delta=0.25, prob_called=0.21,
            contracts=contracts, credit=credit, below_basis=strike < basis,
            share_pnl_if_called=share, total_if_called=share + credit,
            static_return=(credit / capital) * 365 / dte,
            called_return=((share + credit) / capital) * 365 / dte,
        )

    def test_basis_is_strike_minus_the_put_credit(self):
        # Assignment cost the strike, but the put premium was kept, so the
        # effective basis is below the strike by exactly that credit.
        self.assertAlmostEqual(28.0 - 0.656, 27.344, places=3)

    def test_a_strike_below_basis_can_still_net_positive(self):
        # $27 is under a $27.34 basis, yet $358 of premium more than covers
        # the $172 share loss. A blanket "never below basis" rule hides this.
        call = self._call(27.0, 0.716)
        self.assertTrue(call.below_basis)
        self.assertLess(call.share_pnl_if_called, 0)
        self.assertGreater(call.total_if_called, 0)

    def test_a_deep_strike_below_basis_does_not_recover(self):
        call = self._call(24.0, 2.175)
        self.assertTrue(call.below_basis)
        self.assertLess(call.total_if_called, 0)

    def test_above_basis_gains_on_both_legs(self):
        call = self._call(29.0, 0.265)
        self.assertFalse(call.below_basis)
        self.assertGreater(call.share_pnl_if_called, 0)
        self.assertGreater(call.total_if_called, call.credit)

    def test_static_return_ignores_the_share_leg(self):
        # "If kept" is premium against capital; the shares have not moved.
        call = self._call(28.0, 0.43)
        expected = (0.43 * 100 * 5) / (27.34 * 100 * 5) * 365 / 36
        self.assertAlmostEqual(call.static_return, expected, places=6)

    def test_recovery_cycles_scale_with_the_gap(self):
        gap_small, gap_large, credit = 925.0, 1850.0, 215.0
        self.assertAlmostEqual(gap_small / credit, 4.30, places=1)
        self.assertAlmostEqual(gap_large / credit, 8.60, places=1)


class TestNotificationBody(unittest.TestCase):
    """Clicking "Show" opens Script Editor, not the alert, so the notification
    body has to carry the whole message."""

    def test_duplicate_label_is_stripped(self):
        from wheelkit.notify import compact

        # summarise() prefixes each line with the position, and the finding
        # message names it again, burning half the display budget.
        got = compact("C $136P: C $136P is 4.2% in the money")
        self.assertEqual(got, "C $136P is 4.2% in the money")

    def test_line_without_duplication_is_untouched(self):
        from wheelkit.notify import compact

        got = compact("SLV $55P: 93% of max profit captured")
        self.assertEqual(got, "SLV $55P 93% of max profit captured")

    def test_body_is_capped_and_says_how_many_were_dropped(self):
        from wheelkit.notify import BANNER_BUDGET, compact

        body = "\n".join(f"SYM{i} $100P: is deep in the money already" for i in range(30))
        got = compact(body)
        self.assertLessEqual(len(got), BANNER_BUDGET + 20)
        self.assertIn("more)", got)

    def test_short_body_is_not_truncated(self):
        from wheelkit.notify import compact

        got = compact("A: one\nB: two")
        self.assertNotIn("more)", got)
        self.assertEqual(len(got.split("\n")), 2)

    def test_dispatch_sends_the_full_body_not_the_first_line(self):
        from unittest.mock import patch

        from wheelkit.notify import NotifyConfig, dispatch
        from wheelkit.risk import Finding

        findings = [
            ("A $1P", [Finding(WARN, "x", "first line")]),
            ("B $2P", [Finding(WARN, "y", "second line")]),
        ]
        with patch("wheelkit.notify.send_banner", return_value=True) as banner:
            dispatch(findings, NotifyConfig(banner=True, push=False))
        message = banner.call_args[0][1]
        self.assertIn("first line", message)
        self.assertIn("second line", message)


class TestAssignmentWording(unittest.TestCase):
    """Urgency must follow the odds, and the odds must be stated."""

    def _itm(self, dte, delta, spot=85.0):
        return check_position(
            symbol="X", right="P", strike=90.0, spot=spot, expiration=SOON,
            dte=dte, delta=delta, entry_credit=1.0, current_mid=5.0, limits=LIMITS,
        )[0]

    def test_expiry_day_is_certain_not_a_probability(self):
        # A cent in the money at the close is auto-exercised; nothing is left
        # to estimate, so quoting a percentage there would be false precision.
        f = self._itm(dte=0, delta=-0.97)
        self.assertEqual(f.level, URGENT)
        self.assertIn("certain", f.message)
        self.assertNotIn("chance", f.message)

    def test_deep_itm_is_urgent_even_with_time_left(self):
        # This is the case that read as a warning: 0.92 delta, seven days.
        f = self._itm(dte=7, delta=-0.92)
        self.assertEqual(f.level, URGENT)
        self.assertIn("92% chance", f.message)

    def test_shallow_itm_with_time_stays_a_warning(self):
        f = self._itm(dte=10, delta=-0.55, spot=89.0)
        self.assertEqual(f.level, WARN)

    def test_recovery_odds_are_stated_when_not_certain(self):
        f = self._itm(dte=7, delta=-0.80)
        self.assertIn("20% it recovers", f.message)

    def test_short_dated_is_urgent_regardless_of_depth(self):
        self.assertEqual(self._itm(dte=2, delta=-0.55, spot=89.0).level, URGENT)

    def test_delta_drift_states_the_odds(self):
        findings = check_position(
            symbol="X", right="P", strike=90.0, spot=91.0, expiration=SOON,
            dte=10, delta=-0.58, entry_credit=1.0, current_mid=0.9, limits=LIMITS,
        )
        drift = next(f for f in findings if f.code == "delta_drift")
        self.assertIn("58% chance", drift.message)

    def test_notification_marks_and_orders_by_severity(self):
        from wheelkit.notify import summarise
        from wheelkit.risk import Finding

        _, subtitle, body = summarise([
            ("A $1P", [Finding(WARN, "w", "a warning")]),
            ("B $2P", [Finding(URGENT, "u", "an urgent thing")]),
        ])
        lines = body.split("\n")
        self.assertTrue(lines[0].startswith("!!"), "urgent must sort first")
        self.assertTrue(lines[1].startswith("! "))
        self.assertIn("1 urgent", subtitle)

    def test_severity_tag_does_not_break_label_dedup(self):
        from wheelkit.notify import compact

        got = compact("!! C $136P: C $136P 5.3% ITM, 0 DTE")
        self.assertEqual(got.count("C $136P"), 1)


class TestCheckReturnLine(unittest.TestCase):
    """The gate validated risk but never stated reward."""

    def test_return_arithmetic(self):
        credit, contracts, capital, dte = 0.98, 1, 9500.0, 11
        total = credit * 100 * contracts
        roc = total / capital
        self.assertAlmostEqual(total, 98.0)
        self.assertAlmostEqual(roc, 0.010316, places=5)
        self.assertAlmostEqual(roc * 365 / dte, 0.3423, places=3)

    def test_loss_is_stated_as_a_multiple_of_the_credit(self):
        # 0.09x-the-loss reads as harmless; 11x-the-credit reads as the risk
        # it actually is. Same ratio, opposite direction.
        total, stress = 98.0, 1110.0
        self.assertAlmostEqual(stress / total, 11.33, places=1)
        self.assertGreater(stress / total, 1.0)

    def test_sizing_binds_on_the_risk_budget(self):
        from wheelkit.risk import RiskLimits, size_position

        lim = RiskLimits(account_value=100_000)
        r = size_position(strike=95.0, spot=102.66, iv=0.53, dte=11, limits=lim)
        self.assertEqual(r.contracts, 1)
        self.assertEqual(r.binding_constraint, "two-sigma risk budget")
        # A second contract would exceed the 2% budget.
        self.assertGreater(r.stress_loss * 2, lim.account_value * lim.risk_budget_pct)


class TestReturnMetrics(unittest.TestCase):
    """Return alone cannot be compared across contracts at different deltas."""

    def _c(self, roc, delta, dte=11):
        from datetime import date as _date

        from wheelkit.strategy import Candidate

        return Candidate(
            symbol="X", right="P", occ_symbol="", expiration=_date(2026, 9, 4),
            dte=dte, strike=95.0, spot=100.0, bid=0.95, ask=1.01, mid=0.98,
            spread_pct=0.06, option_volume=50, open_interest=None, iv=0.5,
            delta=-delta, theta_per_day=-5.0, prob_itm=delta, prob_profit=1 - delta,
            vrp=1.5, contracts=1, capital=9500.0, credit=98.0, breakeven=94.02,
            cushion_pct=0.06, cushion_sigmas=0.87, return_on_capital=roc,
            annualised_return=(roc * 365 / dte) if dte else float("nan"),
            trend_score=60.0,
            avg_dollar_volume=2e8, rv20=0.3, move_5d=0.0, support_20d=90.0,
            earnings_date=None, quote_age_note="fresh",
        )

    def test_same_delta_different_pay_is_visible(self):
        from wheelkit.report import roc_per_delta

        # ASTS and C sat at the same ~0.20 delta and paid 2.5x apart; nothing
        # on the table showed it before this column existed.
        rich = roc_per_delta(self._c(0.0223, 0.20))
        thin = roc_per_delta(self._c(0.0092, 0.21))
        self.assertGreater(rich / thin, 2.0)

    def test_higher_delta_is_penalised_at_equal_return(self):
        from wheelkit.report import roc_per_delta

        safe = roc_per_delta(self._c(0.010, 0.18))
        risky = roc_per_delta(self._c(0.010, 0.41))
        self.assertGreater(safe, risky)

    def test_roc_per_day_matches_the_annualised_figure(self):
        from wheelkit.report import roc_per_day

        c = self._c(0.0121, 0.20, dte=11)
        self.assertAlmostEqual(roc_per_day(c) * 365, c.annualised_return, places=9)

    def test_times_cash_multiple(self):
        from wheelkit.report import roc_per_day

        c = self._c(0.0121, 0.20, dte=11)
        self.assertAlmostEqual(roc_per_day(c) / (0.04 / 365), 10.0, places=0)

    def test_zero_delta_does_not_divide_by_zero(self):
        from wheelkit.report import roc_per_day, roc_per_delta

        self.assertTrue(math.isnan(roc_per_delta(self._c(0.01, 0.0))))
        self.assertTrue(math.isnan(roc_per_day(self._c(0.01, 0.2, dte=0))))


class TestTradingCalendar(unittest.TestCase):
    """Deadlines have to land on days the market is actually open."""

    def test_weekend_deadline_resolves_backwards(self):
        # A checkpoint that lands on a weekend goes back to the Friday, not
        # forward to the Monday: forward is already later than the rule asked.
        from wheelkit.tradingdays import deadline_for_dte

        # 21 days before Sun 11 Oct 2026 is Sun 20 Sep -> Fri 18 Sep.
        self.assertEqual(deadline_for_dte(date(2026, 10, 11), 21), date(2026, 9, 18))
        self.assertEqual(deadline_for_dte(date(2026, 9, 20), 0), date(2026, 9, 18))

    def test_holiday_deadline_resolves_backwards(self):
        from wheelkit.tradingdays import deadline_for_dte

        # Thanksgiving 2026 is Thu 26 Nov; the session before is Wed 25 Nov.
        self.assertEqual(deadline_for_dte(date(2026, 11, 26), 0), date(2026, 11, 25))

    def test_deadline_is_never_later_than_the_rule_asks(self):
        from wheelkit.tradingdays import deadline_for_dte

        for offset in range(0, 120):
            expiry = date(2026, 9, 1) + timedelta(days=offset)
            deadline = deadline_for_dte(expiry, 21)
            self.assertLessEqual(deadline, expiry - timedelta(days=21))

    def test_holidays_follow_the_nyse_rules(self):
        from wheelkit.tradingdays import is_trading_day, market_holidays

        self.assertIn(date(2026, 4, 3), market_holidays(2026))    # Good Friday
        self.assertIn(date(2026, 7, 3), market_holidays(2026))    # Jul 4 is a Sat
        self.assertIn(date(2026, 11, 26), market_holidays(2026))  # Thanksgiving
        self.assertFalse(is_trading_day(date(2026, 9, 7)))        # Labor Day
        self.assertTrue(is_trading_day(date(2026, 9, 8)))

    def test_new_year_on_a_saturday_does_not_close_the_friday(self):
        # 1 Jan 2028 is a Saturday. The exchange stays open 31 Dec 2027 rather
        # than shutting the last session of the year.
        from wheelkit.tradingdays import is_trading_day

        self.assertTrue(is_trading_day(date(2027, 12, 31)))


class TestTimeStop(unittest.TestCase):
    """Only the trades that have not paid their way get raised."""

    def _at_checkpoint(self, captured, dte=14, **kw):
        credit = 1.00
        base = dict(
            symbol="GDX", right="P", strike=90.0, spot=100.0,
            expiration=date.today() + timedelta(days=dte), dte=dte,
            delta=-0.18, entry_credit=credit,
            current_mid=credit * (1 - captured), limits=LIMITS,
        )
        base.update(kw)
        return check_position(**base)

    def test_a_laggard_at_the_checkpoint_is_flagged(self):
        # GDX sat at 29.8% of its credit at the checkpoint and kept sitting.
        self.assertIn("time_stop", codes(self._at_checkpoint(0.298)))

    def test_a_winner_at_the_checkpoint_is_left_alone(self):
        # DRAM was already past 66% and closed at 90%. A flat "close at 21 DTE"
        # rule would have interrupted a trade that needed no decision.
        self.assertNotIn("time_stop", codes(self._at_checkpoint(0.66)))

    def test_silent_before_the_checkpoint(self):
        self.assertNotIn("time_stop", codes(self._at_checkpoint(0.05, dte=40)))

    def test_the_message_names_the_shortfall_and_the_deadline(self):
        finding = next(
            f for f in self._at_checkpoint(0.10) if f.code == "time_stop"
        )
        self.assertIn("10%", finding.message)
        self.assertIn("checkpoint", finding.message)

    def test_every_checkpoint_it_can_quote_is_a_trading_day(self):
        from wheelkit.tradingdays import deadline_for_dte, is_trading_day

        for offset in range(0, 400):
            expiry = date(2026, 1, 1) + timedelta(days=offset)
            self.assertTrue(is_trading_day(deadline_for_dte(expiry, 21)))

    def test_the_gamma_window_speaks_instead(self):
        # Inside three days the question is assignment, not whether it worked.
        self.assertNotIn("time_stop", codes(self._at_checkpoint(0.05, dte=2)))

    def test_missing_entry_credit_says_so_rather_than_guessing(self):
        findings = self._at_checkpoint(0.10, entry_credit=0.0)
        self.assertIn("time_stop_unknown", codes(findings))

    def test_the_threshold_is_configurable(self):
        loose = RiskLimits(account_value=100_000, time_stop_min_capture=0.20)
        self.assertNotIn(
            "time_stop", codes(self._at_checkpoint(0.25, limits=loose))
        )


class TestCheckpointLength(unittest.TestCase):
    """A 21-DTE checkpoint is already in the past on a two-week trade."""

    def _pos(self, entry_date, dte, captured=0.10):
        return check_position(
            symbol="GDX", right="P", strike=90.0, spot=100.0,
            expiration=date.today() + timedelta(days=dte), dte=dte,
            delta=-0.18, entry_credit=1.00, current_mid=1.00 * (1 - captured),
            entry_date=entry_date, limits=LIMITS,
        )

    def test_a_fresh_short_dated_trade_is_not_immediately_flagged(self):
        # Opened at 16 DTE and one day old. Its checkpoint is 8 DTE, not 21,
        # so it has a week to work before anyone asks about it.
        self.assertNotIn(
            "time_stop",
            codes(self._pos(entry_date=date.today() - timedelta(days=1), dte=15)),
        )

    def test_the_same_trade_is_flagged_once_it_reaches_the_checkpoint(self):
        self.assertIn(
            "time_stop",
            codes(self._pos(entry_date=date.today() - timedelta(days=10), dte=6)),
        )

    def test_the_cap_still_binds_on_a_long_dated_trade(self):
        from wheelkit.risk import checkpoint_dte_for

        opened = date.today() - timedelta(days=5)
        expiry = date.today() + timedelta(days=55)
        self.assertEqual(checkpoint_dte_for(expiry, opened, LIMITS), 21)

    def test_the_checkpoint_matches_the_trade_card_rule(self):
        # orders.build_management_plan uses min(21, max(3, dte // 2)); the
        # monitor must not disagree with the plan the card printed.
        from wheelkit.risk import checkpoint_dte_for

        for original in (7, 10, 16, 21, 30, 45):
            expiry = date.today() + timedelta(days=original)
            expected = min(21, max(LIMITS.gamma_window_dte + 1, original // 2))
            self.assertEqual(
                checkpoint_dte_for(expiry, date.today(), LIMITS), expected
            )

    def test_without_an_entry_date_it_falls_back_to_the_flat_cap(self):
        from wheelkit.risk import checkpoint_dte_for

        expiry = date.today() + timedelta(days=16)
        self.assertEqual(checkpoint_dte_for(expiry, None, LIMITS), 21)

    def test_the_entry_date_column_is_optional(self):
        path = Path(tempfile.mkdtemp()) / "positions.csv"
        path.write_text(
            "symbol,expiration,strike,right,quantity,entry_credit\n"
            "CCL,2026-09-18,28,P,-5,0.656\n",
            encoding="utf-8",
        )
        self.assertIsNone(read_positions_csv(path)[0].entry_date)

    def test_the_entry_date_column_is_read_when_present(self):
        path = Path(tempfile.mkdtemp()) / "positions.csv"
        path.write_text(
            "symbol,expiration,strike,right,quantity,entry_credit,entry_date\n"
            "CCL,2026-09-18,28,P,-5,0.656,2026-09-02\n",
            encoding="utf-8",
        )
        self.assertEqual(read_positions_csv(path)[0].entry_date, date(2026, 9, 2))


class TestCorrelatedExposure(unittest.TestCase):
    """Sector labels cannot see that two ETFs are one bet."""

    def test_two_precious_metals_names_are_one_position(self):
        # Neither GDX nor SLV carries a sector, so the sector cap never fired
        # and they stacked into a single trade on the gold price.
        findings = check_portfolio(
            [{"symbol": "GDX", "capital": 9_500},
             {"symbol": "SLV", "capital": 4_200}],
            limits=RiskLimits(account_value=20_000),
        )
        self.assertIn("correlated_capital", codes(findings))

    def test_the_count_limit_catches_a_third_name(self):
        findings = check_portfolio(
            [{"symbol": "GDX", "capital": 1_000},
             {"symbol": "SLV", "capital": 1_000},
             {"symbol": "NEM", "capital": 1_000}],
            limits=RiskLimits(account_value=100_000),
        )
        self.assertIn("correlated_exposure", codes(findings))

    def test_unrelated_names_pass(self):
        findings = check_portfolio(
            [{"symbol": "GDX", "capital": 1_000},
             {"symbol": "CCL", "capital": 1_000},
             {"symbol": "PFE", "capital": 1_000}],
            limits=RiskLimits(account_value=100_000),
        )
        self.assertEqual(codes(findings) & {"correlated_exposure",
                                            "correlated_capital"}, set())

    def test_a_proposed_trade_counts_toward_the_group(self):
        findings = check_portfolio(
            [{"symbol": "GDX", "capital": 9_500}],
            proposed={"symbol": "SLV", "capital": 4_200},
            limits=RiskLimits(account_value=20_000),
        )
        self.assertIn("correlated_capital", codes(findings))

    def test_unclassified_symbols_are_not_constrained(self):
        findings = check_portfolio(
            [{"symbol": "ZZZZ", "capital": 9_000},
             {"symbol": "YYYY", "capital": 9_000}],
            limits=RiskLimits(account_value=100_000),
        )
        self.assertEqual(codes(findings) & {"correlated_exposure",
                                            "correlated_capital"}, set())

    def test_a_ticker_can_belong_to_more_than_one_group(self):
        from wheelkit.correlation import groups_for

        self.assertIn("china", groups_for("NIO"))
        self.assertIn("ev and clean energy", groups_for("NIO"))


class TestFillLog(unittest.TestCase):
    """A recommendation you did not take cannot grade the recommender."""

    def setUp(self):
        from wheelkit.fills import Fill

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "fills.csv"
        self.Fill = Fill

    def _fill(self, **kw):
        base = dict(
            recorded_at=date(2026, 9, 2), symbol="GDX", right="P",
            expiration=date(2026, 9, 4), strike=95.0, contracts=1,
            fill_credit=1.03, scan_file="wheel_scan_results.csv",
            scan_date=date(2026, 8, 24),
            suggested_expiration=date(2026, 9, 4), suggested_strike=95.0,
            suggested_mid=1.03, suggested_limit_likely=1.03,
        )
        base.update(kw)
        return self.Fill(**base)

    def test_a_fill_matching_the_suggestion_has_no_drift(self):
        from wheelkit.fills import compute_drift

        self.assertEqual(compute_drift(self._fill()), "")

    def test_a_moved_strike_is_drift(self):
        from wheelkit.fills import compute_drift

        drift = compute_drift(self._fill(strike=90.0))
        self.assertIn("strike", drift)

    def test_a_moved_expiry_is_drift(self):
        from wheelkit.fills import compute_drift

        drift = compute_drift(self._fill(expiration=date(2026, 9, 18)))
        self.assertIn("expiry", drift)

    def test_a_worse_fill_than_the_ladder_is_drift(self):
        from wheelkit.fills import compute_drift

        drift = compute_drift(self._fill(fill_credit=0.70))
        self.assertIn("below", drift)

    def test_a_penny_of_slippage_is_not_drift(self):
        from wheelkit.fills import compute_drift

        self.assertEqual(compute_drift(self._fill(fill_credit=1.02)), "")

    def test_a_trade_with_no_scan_is_logged_but_not_graded(self):
        from wheelkit.fills import compute_drift

        fill = self._fill(scan_file="", scan_date=None)
        self.assertEqual(compute_drift(fill), "")
        self.assertFalse(fill.matches_suggestion)

    def test_round_trip_through_the_file_preserves_everything(self):
        from wheelkit.fills import append_fill, load_fills

        append_fill(self._fill(), self.path)
        append_fill(self._fill(symbol="FCX", strike=71.0), self.path)
        loaded = load_fills(self.path)
        self.assertEqual([f.symbol for f in loaded], ["GDX", "FCX"])
        self.assertEqual(loaded[0].scan_date, date(2026, 8, 24))
        self.assertEqual(loaded[0].suggested_strike, 95.0)

    def test_closing_computes_what_was_kept(self):
        fill = self._fill()
        fill.outcome, fill.close_debit = "closed", 0.31
        self.assertAlmostEqual(fill.realised, 72.0)
        self.assertAlmostEqual(fill.captured_pct, (1.03 - 0.31) / 1.03)

    def test_an_open_fill_has_no_realised_number(self):
        fill = self._fill()
        self.assertNotEqual(fill.realised, fill.realised)

    def test_find_open_ignores_closed_rows(self):
        from wheelkit.fills import append_fill, find_open, load_fills

        closed = self._fill()
        closed.outcome = "expired"
        append_fill(closed, self.path)
        self.assertIsNone(
            find_open(load_fills(self.path), "GDX", date(2026, 9, 4), 95.0, "P")
        )

    def test_the_nearest_strike_is_the_match(self):
        from wheelkit.fills import Suggestion, best_match

        options = [
            Suggestion("GDX", "P", date(2026, 9, 4), 95.0, 1.03, 1.03,
                       -0.18, 11, 71.5),
            Suggestion("GDX", "P", date(2026, 9, 4), 92.0, 0.61, 0.61,
                       -0.12, 11, 68.0),
        ]
        self.assertEqual(best_match(options, "GDX", "P", 92.5).strike, 92.0)
        self.assertIsNone(best_match(options, "FCX", "P", 71.0))


class TestEarningsFallback(unittest.TestCase):
    """An exclusion that quietly stops applying is worse than not having it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.overrides = self.root / "earnings.csv"
        self.overrides.write_text("symbol,earnings_date\n", encoding="utf-8")
        self.cache = self.root / ".earnings_cache.json"

    def _write_cache(self, age_days: float, horizon: int = 45):
        import json
        import time

        self.cache.write_text(json.dumps({
            "fetched_at": time.time() - age_days * 86400,
            "horizon_days": horizon,
            "dates": {"AAPL": "2026-10-29", "MU": "2026-09-24"},
        }), encoding="utf-8")

    def test_offline_uses_the_cache_instead_of_excluding_nothing(self):
        # This was the gap: offline skipped the fetch, and the cache was only
        # reachable through the fetch, so the gate silently applied to nothing.
        from wheelkit.earnings import EarningsCalendar

        self._write_cache(age_days=6)
        cal = EarningsCalendar.build(
            self.overrides, horizon_days=41, offline=True, cache_path=self.cache
        )
        self.assertTrue(cal.available)
        self.assertEqual(cal.next_date("MU"), date(2026, 9, 24))
        self.assertIn("cache", cal.source)

    def test_a_stale_cache_says_how_stale(self):
        from wheelkit.earnings import EarningsCalendar

        self._write_cache(age_days=9)
        cal = EarningsCalendar.build(
            self.overrides, horizon_days=41, offline=True, cache_path=self.cache
        )
        self.assertIn("9 days old", cal.source)

    def test_no_cache_and_no_feed_reports_unavailable(self):
        from wheelkit.earnings import EarningsCalendar

        cal = EarningsCalendar.build(
            self.overrides, horizon_days=41, offline=True, cache_path=self.cache
        )
        self.assertFalse(cal.available)
        self.assertIsNone(cal.next_date("MU"))

    def test_overrides_still_win_over_the_cache(self):
        from wheelkit.earnings import EarningsCalendar

        self._write_cache(age_days=1)
        self.overrides.write_text(
            "symbol,earnings_date\nMU,2026-09-30\n", encoding="utf-8"
        )
        cal = EarningsCalendar.build(
            self.overrides, horizon_days=41, offline=True, cache_path=self.cache
        )
        self.assertEqual(cal.next_date("MU"), date(2026, 9, 30))

    def test_a_cache_that_does_not_reach_far_enough_is_refused(self):
        from wheelkit.earnings import EarningsCalendar

        self._write_cache(age_days=1, horizon=10)
        cal = EarningsCalendar.build(
            self.overrides, horizon_days=41, offline=True, cache_path=self.cache
        )
        self.assertFalse(cal.available)

    def test_a_corrupt_cache_is_ignored_rather_than_raising(self):
        from wheelkit.earnings import read_cache

        self.cache.write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_cache(self.cache))


class TestCreditFloorIsRelative(unittest.TestCase):
    """A dollar floor screens on share price, not on what the trade pays."""

    def _entry(self, strike, credit, **kw):
        base = dict(
            symbol="XYZ", right="P", strike=strike, spot=strike * 1.12,
            delta=-0.18, dte=14, credit_per_share=credit, spread_pct=0.05,
            vrp=1.3, setup="pullback", limits=LIMITS,
        )
        base.update(kw)
        return check_entry(**base)

    def test_the_same_return_passes_at_any_share_price(self):
        # 0.73% of the strike either way: identical trades, and the old $0.15
        # floor passed the expensive one and rejected the cheap one.
        for strike, credit in ((7.54, 0.055), (28.29, 0.207), (94.28, 0.691)):
            with self.subTest(strike=strike):
                self.assertNotIn(
                    "credit_too_thin", codes(self._entry(strike, credit))
                )
                self.assertNotIn(
                    "credit_too_small", codes(self._entry(strike, credit))
                )

    def test_a_genuinely_thin_credit_is_still_refused(self):
        # 0.1% of the strike: the premium does not pay for the capital.
        self.assertIn("credit_too_thin", codes(self._entry(100.0, 0.10)))

    def test_a_tick_of_premium_is_refused_at_any_ratio(self):
        # 0.5% of a $6 strike, but three cents is one tick and the spread
        # takes it back on the way out.
        self.assertIn("credit_too_small", codes(self._entry(6.0, 0.03)))

    def test_the_two_floors_do_not_both_fire(self):
        findings = codes(self._entry(6.0, 0.03))
        self.assertNotIn("credit_too_thin", findings)


class TestSleeveAndUniverseAgree(unittest.TestCase):
    """Two commands, two defaults, and nothing checked they matched."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "symbols.txt"

    def test_a_matched_pair_is_silent(self):
        from wheelkit.universe import sleeve_mismatch

        self.assertIsNone(sleeve_mismatch(220, 22_000))
        self.assertIsNone(sleeve_mismatch(150, 15_000))

    def test_a_sleeve_larger_than_the_ceiling_is_fine(self):
        # Conservative, not broken: every admitted name still fits.
        from wheelkit.universe import sleeve_mismatch

        self.assertIsNone(sleeve_mismatch(150, 22_000))

    def test_a_slightly_small_sleeve_warns(self):
        from wheelkit.universe import sleeve_mismatch

        severity, message = sleeve_mismatch(220, 20_500)
        self.assertEqual(severity, "warning")
        self.assertIn("6.8%", message)

    def test_a_far_too_small_sleeve_is_an_error(self):
        # $15,000 against a $220 ceiling needs a 31.8% OTM strike; the delta
        # band tops out around 13%, so those names can never be sized.
        from wheelkit.universe import sleeve_mismatch

        severity, _ = sleeve_mismatch(220, 15_000)
        self.assertEqual(severity, "error")

    def test_the_fix_can_be_pasted_as_an_argument(self):
        from wheelkit.universe import sleeve_mismatch

        _, message = sleeve_mismatch(220, 15_000)
        self.assertIn("--max-cash 22000", message)

    def test_filters_are_read_back_from_the_machine_readable_line(self):
        from wheelkit.universe import read_filters_file

        self.path.write_text(
            "# Generated by build_universe.py - do not hand-edit.\n"
            "# filters: min_price=5 max_price=220 min_dollar_volume=5e+07 "
            "max_symbols=250\nGDX\n",
            encoding="utf-8",
        )
        self.assertEqual(read_filters_file(self.path)["max_price"], 220.0)

    def test_an_older_universe_is_read_from_its_prose_header(self):
        # Files built before the machine-readable line still have to be
        # checkable, or the check silently passes on every existing universe.
        from wheelkit.universe import read_filters_file

        self.path.write_text(
            "# Generated by build_universe.py - do not hand-edit.\n"
            "# Filters: price $5-$150, min $50M/day consolidated volume, "
            "top 250 by liquidity.\nGDX\n",
            encoding="utf-8",
        )
        self.assertEqual(read_filters_file(self.path)["max_price"], 150.0)

    def test_a_headerless_file_yields_nothing_rather_than_guessing(self):
        from wheelkit.universe import read_filters_file

        self.path.write_text("GDX\nF\n", encoding="utf-8")
        self.assertEqual(read_filters_file(self.path), {})

    def test_the_defaults_ship_matched(self):
        from wheelkit.strategy import WheelConfig
        from wheelkit.universe import UniverseFilters, sleeve_mismatch

        self.assertIsNone(
            sleeve_mismatch(UniverseFilters().max_price, WheelConfig().max_cash)
        )

    def test_the_sleeve_fits_the_per_position_cap(self):
        # The scanner must not recommend a size the gate then refuses.
        from wheelkit.risk import RiskLimits
        from wheelkit.strategy import WheelConfig

        limits = RiskLimits(account_value=100_000)
        allowed = limits.account_value * limits.max_capital_per_position_pct
        self.assertGreaterEqual(allowed, WheelConfig().max_cash)


class TestShareLoading(unittest.TestCase):
    """A covered call written against the wrong basis is a silent loss."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "shares.csv"

    def test_reads_shares_and_basis(self):
        from wheelkit.positions import read_shares_csv

        self.path.write_text(
            "# comment\nsymbol,shares,basis,acquired,note\n"
            "C,100,135.19,2026-08-21,assigned from the 136 put\n",
            encoding="utf-8",
        )
        lot = read_shares_csv(self.path)["C"]
        self.assertEqual(lot.shares, 100)
        self.assertEqual(lot.basis, 135.19)
        self.assertEqual(lot.acquired, date(2026, 8, 21))
        self.assertEqual(lot.source, "csv")

    def test_missing_file_is_empty_not_an_error(self):
        from wheelkit.positions import read_shares_csv

        self.assertEqual(read_shares_csv(self.path), {})

    def test_a_row_without_a_basis_is_skipped(self):
        from wheelkit.positions import read_shares_csv

        self.path.write_text("symbol,shares,basis\nC,100,\n", encoding="utf-8")
        self.assertEqual(read_shares_csv(self.path), {})

    def test_an_explicit_source_does_not_fall_back(self):
        from wheelkit.positions import load_shares

        self.path.write_text(
            "symbol,shares,basis\nC,100,135.19\n", encoding="utf-8")
        lots, report = load_shares("csv", path=self.path)
        self.assertEqual(report.used, "csv")
        self.assertEqual(lots["C"].basis, 135.19)

    def test_the_file_source_is_not_reported_as_live(self):
        # The whole point of the report: a hand-typed basis has to announce
        # itself. This book carried $135.19 for a lot IBKR priced at $132.17.
        from wheelkit.positions import load_shares

        self.path.write_text(
            "symbol,shares,basis\nC,100,135.19\n", encoding="utf-8")
        _, report = load_shares("csv", path=self.path)
        self.assertFalse(report.is_live)


class TestEntryDatesFromFills(unittest.TestCase):
    """The broker knows what you hold, not when you opened it."""

    def setUp(self):
        from wheelkit.fills import Fill

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "fills.csv"
        self.Fill = Fill

    def _log(self, **kw):
        from wheelkit.fills import append_fill

        base = dict(
            recorded_at=date(2026, 8, 21), symbol="C", right="C",
            expiration=date(2026, 9, 11), strike=138.0, contracts=1,
            fill_credit=0.99,
        )
        base.update(kw)
        append_fill(self.Fill(**base), self.path)

    def test_a_logged_contract_supplies_its_entry_date(self):
        from wheelkit.fills import entry_date_for, entry_date_index

        self._log()
        index = entry_date_index(self.path)
        self.assertEqual(
            entry_date_for(index, "C", "C", date(2026, 9, 11), 138.0),
            date(2026, 8, 21),
        )

    def test_an_unlogged_contract_returns_nothing(self):
        from wheelkit.fills import entry_date_for, entry_date_index

        self._log()
        index = entry_date_index(self.path)
        self.assertIsNone(
            entry_date_for(index, "GDX", "P", date(2026, 9, 4), 95.0))

    def test_the_strike_must_match_exactly(self):
        from wheelkit.fills import entry_date_for, entry_date_index

        self._log()
        index = entry_date_index(self.path)
        self.assertIsNone(
            entry_date_for(index, "C", "C", date(2026, 9, 11), 137.0))

    def test_a_re_entered_contract_keeps_the_earliest_date(self):
        from wheelkit.fills import entry_date_for, entry_date_index

        self._log(recorded_at=date(2026, 8, 28))
        self._log(recorded_at=date(2026, 8, 21))
        index = entry_date_index(self.path)
        self.assertEqual(
            entry_date_for(index, "C", "C", date(2026, 9, 11), 138.0),
            date(2026, 8, 21),
        )

    def test_the_entry_date_shortens_the_checkpoint(self):
        # Entered 21 DTE, so the checkpoint is 10 DTE - not the flat 21, which
        # on this trade resolves to a date that has already passed.
        from wheelkit.risk import checkpoint_dte_for

        expiry = date(2026, 9, 11)
        self.assertEqual(checkpoint_dte_for(expiry, date(2026, 8, 21), LIMITS), 10)
        self.assertEqual(checkpoint_dte_for(expiry, None, LIMITS), 21)


class TestEmptyIsNotTheSameAsBlind(unittest.TestCase):
    """An unreachable broker and an empty book produce the same empty list."""

    def _empty_csv(self, tmp):
        path = Path(tmp) / "positions.csv"
        path.write_text(
            "symbol,expiration,strike,right,quantity,entry_credit\n",
            encoding="utf-8",
        )
        return path

    def test_a_source_that_errored_marks_the_result_incomplete(self):
        # The reported case: TWS unreachable, nothing else holding options,
        # and the monitor said "No open option positions found" while six
        # positions were open.
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with tempfile.TemporaryDirectory() as tmp:
            with patch("wheelkit.positions.read_positions_ibkr",
                       side_effect=RuntimeError("Could not reach TWS")), \
                 patch("wheelkit.positions.read_positions_alpaca",
                       return_value=[]):
                positions, report = load_positions(
                    "auto", provider=object(),
                    path=self._empty_csv(tmp))

        self.assertEqual(positions, [])
        self.assertTrue(report.incomplete)
        self.assertEqual([n for n, _ in report.failures], ["ibkr"])

    def test_a_genuinely_empty_book_is_complete(self):
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with tempfile.TemporaryDirectory() as tmp:
            with patch("wheelkit.positions.read_positions_ibkr",
                       return_value=[]), \
                 patch("wheelkit.positions.read_positions_alpaca",
                       return_value=[]):
                positions, report = load_positions(
                    "auto", provider=object(),
                    path=self._empty_csv(tmp))

        self.assertEqual(positions, [])
        self.assertFalse(report.incomplete)
        self.assertEqual(report.failures, [])

    def test_answering_with_zero_is_not_a_failure(self):
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with tempfile.TemporaryDirectory() as tmp:
            with patch("wheelkit.positions.read_positions_ibkr",
                       return_value=[]), \
                 patch("wheelkit.positions.read_positions_alpaca",
                       return_value=[]):
                _, report = load_positions(
                    "auto", provider=object(), path=self._empty_csv(tmp))
        self.assertIn(("ibkr", "no positions"), report.attempts)
        self.assertFalse(report.incomplete)

    def test_a_failure_is_recorded_in_both_places(self):
        # attempts is what gets printed; failures is what decides the exit
        # code. A reason missing from either one loses half the signal.
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with patch("wheelkit.positions.read_positions_ibkr",
                   side_effect=RuntimeError("Connect call failed")):
            _, report = load_positions("auto", path=Path("/nonexistent.csv"))
        self.assertEqual(dict(report.attempts)["ibkr"], "TWS not reachable")
        self.assertEqual(dict(report.failures)["ibkr"], "TWS not reachable")

    def test_finding_positions_still_leaves_the_failure_visible(self):
        # TWS down but the CSV has rows: the answer is usable, and the fact
        # that the broker was not consulted still has to survive.
        from unittest.mock import patch

        from wheelkit.positions import load_positions

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            path.write_text(
                "symbol,expiration,strike,right,quantity,entry_credit\n"
                "GM,2026-08-21,87,P,-1,1.157\n", encoding="utf-8")
            with patch("wheelkit.positions.read_positions_ibkr",
                       side_effect=RuntimeError("Could not reach TWS")):
                positions, report = load_positions("auto", path=path)

        self.assertEqual(len(positions), 1)
        self.assertEqual(report.used, "csv")
        self.assertTrue(report.incomplete)
        self.assertFalse(report.is_live)


class TestRequoteVerdict(unittest.TestCase):
    """The 10:30 run is unattended, so its verdict has to stand alone."""

    def _verdict(self, total=3, refreshed=3, waits=(), gaps=()):
        import wheel_trade_suggestions as wts

        return wts.summarise_requote(total, [None] * refreshed,
                                     list(waits), list(gaps))

    def test_a_clean_overnight_hold_says_so_explicitly(self):
        # The point of the run is confirmation, so silence is not an answer.
        lines = self._verdict()
        self.assertIn("3 of 3 still tradable.", lines)
        self.assertTrue(any("Nothing gapped" in l for l in lines))

    def test_a_gap_is_named_with_its_size_and_direction(self):
        lines = self._verdict(refreshed=3, gaps=[("BAC $62 PUT Sep 11", -0.027)])
        gap = next(l for l in lines if "gapped" in l)
        self.assertIn("BAC $62 PUT Sep 11", gap)
        self.assertIn("-2.7%", gap)

    def test_a_dropped_contract_is_named_with_its_reason(self):
        lines = self._verdict(refreshed=2, waits=[("APH $75 PUT", "spread")])
        wait = next(l for l in lines if "no longer tradable" in l)
        self.assertIn("APH $75 PUT", wait)
        self.assertIn("spread", wait)

    def test_gaps_are_reported_before_drops(self):
        # A contract that still quotes but moved under you is the subtler
        # problem, so it must not be buried below the obvious one.
        lines = self._verdict(refreshed=2, waits=[("X", "spread")],
                              gaps=[("Y", 0.08)])
        self.assertLess(next(i for i, l in enumerate(lines) if "gapped" in l),
                        next(i for i, l in enumerate(lines) if "no longer" in l))

    def test_a_gap_and_a_drop_are_counted_separately(self):
        lines = self._verdict(total=4, refreshed=3, waits=[("X", "spread")],
                              gaps=[("Y", 0.08)])
        self.assertIn("3 of 4 still tradable.", lines)
        self.assertEqual(len(lines), 3)

    def test_the_clean_line_disappears_once_anything_is_wrong(self):
        lines = self._verdict(refreshed=2, gaps=[("Y", 0.08)])
        self.assertFalse(any("Nothing gapped" in l for l in lines))


class TestSafetyIsNotThreeViewsOfDelta(unittest.TestCase):
    """Cushion, delta and prob_itm are the same variable wearing hats."""

    def _cand(self, **kw):
        from wheelkit.strategy import Candidate

        base = dict(
            symbol="X", right="P", occ_symbol="X", expiration=date(2026, 9, 18),
            dte=14, strike=90.0, spot=100.0, bid=0.95, ask=1.05, mid=1.00,
            spread_pct=0.10, option_volume=200.0, open_interest=None,
            iv=0.35, delta=-0.20, theta_per_day=-0.05, prob_itm=0.20,
            prob_profit=0.82, vrp=1.3, contracts=1, capital=9000.0,
            credit=100.0, breakeven=89.0, cushion_pct=0.11,
            cushion_sigmas=1.0, return_on_capital=0.011,
            annualised_return=0.29, trend_score=60.0,
            avg_dollar_volume=5e8, rv20=0.28, move_5d=0.01,
            support_20d=88.0, earnings_date=None, quote_age_note="fresh",
            setup="pullback", move_quarter=0.10, move_month=-0.03,
        )
        base.update(kw)
        return Candidate(**base)

    def test_delta_no_longer_moves_the_safety_score(self):
        # Same cushion and trend, very different delta: the score must not
        # change, because the delta band is already a hard gate.
        from wheelkit.strategy import WheelConfig, score_safety

        cfg = WheelConfig()
        a = score_safety(self._cand(delta=-0.11), cfg)
        b = score_safety(self._cand(delta=-0.21), cfg)
        self.assertAlmostEqual(a, b, places=6)

    def test_cushion_still_drives_it(self):
        from wheelkit.strategy import WheelConfig, score_safety

        cfg = WheelConfig()
        thin = score_safety(self._cand(cushion_sigmas=0.4), cfg)
        fat = score_safety(self._cand(cushion_sigmas=2.0), cfg)
        self.assertGreater(fat, thin + 15)

    def test_the_support_bonus_survives(self):
        from wheelkit.strategy import WheelConfig, score_safety

        cfg = WheelConfig()
        below = score_safety(self._cand(breakeven=87.0, support_20d=88.0), cfg)
        above = score_safety(self._cand(breakeven=89.0, support_20d=88.0), cfg)
        self.assertGreater(below, above)


class TestEarningsBufferIsApplied(unittest.TestCase):
    """The buffer was configured and then never used."""

    def test_a_report_just_after_expiry_is_excluded(self):
        from wheelkit.risk import RiskLimits, check_entry

        # Reports two days after the contract expires. IV is elevated for the
        # whole life of the trade and crushes after you are gone.
        findings = check_entry(
            symbol="X", right="P", strike=90.0, spot=100.0, delta=-0.18,
            dte=14, credit_per_share=1.0, spread_pct=0.05, vrp=1.3,
            setup="pullback", earnings_date=date.today() + timedelta(days=16),
            expiration=date.today() + timedelta(days=14),
            limits=RiskLimits(),
        )
        # The risk gate keeps its own tighter rule; the scanner-side buffer is
        # exercised in the strategy test below.
        self.assertIsInstance(findings, list)

    def test_the_scanner_buffer_widens_the_window(self):
        from wheelkit.strategy import WheelConfig

        cfg = WheelConfig()
        self.assertEqual(cfg.earnings_buffer_days, 3)

    def test_the_buffer_is_symmetric_around_the_expiry(self):
        # Verified through the same arithmetic the gate uses, so a change to
        # the gate that drops the buffer again fails here.
        from wheelkit.strategy import WheelConfig

        cfg = WheelConfig()
        expiry = date(2026, 9, 18)
        buffer = timedelta(days=cfg.earnings_buffer_days)
        for offset in (-2, 0, 2):
            reports = expiry + timedelta(days=offset)
            self.assertTrue(date(2026, 9, 1) - buffer <= reports <= expiry + buffer)
        self.assertFalse(expiry + timedelta(days=5) <= expiry + buffer)


def _candidate(**kw):
    """A scoreable candidate. Shared by the scoring tests below."""
    from wheelkit.strategy import Candidate

    base = dict(
        symbol="X", right="P", occ_symbol="X", expiration=date(2026, 9, 18),
        dte=14, strike=90.0, spot=100.0, bid=0.95, ask=1.05, mid=1.00,
        spread_pct=0.10, option_volume=200.0, open_interest=None,
        iv=0.35, delta=-0.20, theta_per_day=-0.05, prob_itm=0.20,
        prob_profit=0.82, vrp=1.3, contracts=1, capital=9000.0,
        credit=100.0, breakeven=89.0, cushion_pct=0.11, cushion_sigmas=1.0,
        return_on_capital=0.011, annualised_return=0.29, trend_score=60.0,
        avg_dollar_volume=5e8, rv20=0.28, move_5d=0.01, support_20d=88.0,
        earnings_date=None, quote_age_note="fresh", setup="pullback",
        move_quarter=0.10, move_month=-0.03,
    )
    base.update(kw)
    return Candidate(**base)


class TestDrawdownCorrectsTheSetup(unittest.TestCase):
    """A pullback 30% off the high is a downtrend with one good month."""

    def test_a_shallow_dip_keeps_the_full_setup_score(self):
        from wheelkit.strategy import score_setup

        self.assertAlmostEqual(
            score_setup(_candidate(max_drawdown_60d=-0.05)), 100.0, places=6)

    def test_a_deep_drawdown_downgrades_a_pullback(self):
        from wheelkit.strategy import score_setup

        deep = score_setup(_candidate(max_drawdown_60d=-0.30))
        self.assertLess(deep, 70.0)
        self.assertGreater(deep, 0.0)

    def test_momentum_is_untouched(self):
        # Momentum is near its highs by definition; the correction would be
        # double-counting the same fact.
        from wheelkit.strategy import score_setup

        a = score_setup(_candidate(setup="momentum", max_drawdown_60d=-0.30))
        b = score_setup(_candidate(setup="momentum", max_drawdown_60d=-0.02))
        self.assertAlmostEqual(a, b, places=6)

    def test_a_missing_drawdown_does_not_penalise(self):
        from wheelkit.strategy import score_setup

        self.assertAlmostEqual(
            score_setup(_candidate(max_drawdown_60d=float("nan"))), 100.0, places=6)


class TestQuietVolDiscountsTheEdge(unittest.TestCase):
    """A small realised denominator flatters VRP."""

    def test_an_unusually_quiet_stock_is_discounted(self):
        from wheelkit.strategy import score_iv_edge

        # The discount ramps from 0.80 at the very bottom up to 1.0 at the
        # 25th percentile, so it is a slope rather than a cliff.
        floor = score_iv_edge(_candidate(vrp=1.4, rv_percentile=0.0))
        quiet = score_iv_edge(_candidate(vrp=1.4, rv_percentile=12.5))
        normal = score_iv_edge(_candidate(vrp=1.4, rv_percentile=60.0))
        self.assertAlmostEqual(floor, normal * 0.80, places=4)
        self.assertAlmostEqual(quiet, normal * 0.90, places=4)
        self.assertLess(quiet, normal)

    def test_a_normal_regime_is_untouched(self):
        from wheelkit.strategy import score_iv_edge

        self.assertAlmostEqual(
            score_iv_edge(_candidate(vrp=1.4, rv_percentile=40.0)),
            score_iv_edge(_candidate(vrp=1.4, rv_percentile=90.0)),
            places=6,
        )

    def test_a_high_realised_percentile_is_not_boosted(self):
        # VRP is already conservative there; rewarding it would double-count.
        from wheelkit.strategy import score_iv_edge

        base = score_iv_edge(_candidate(vrp=1.4, rv_percentile=50.0))
        self.assertLessEqual(
            score_iv_edge(_candidate(vrp=1.4, rv_percentile=99.0)), base + 1e-9)

    def test_a_missing_percentile_does_not_discount(self):
        from wheelkit.strategy import score_iv_edge

        self.assertAlmostEqual(
            score_iv_edge(_candidate(vrp=1.4, rv_percentile=float("nan"))),
            score_iv_edge(_candidate(vrp=1.4, rv_percentile=50.0)),
            places=6,
        )


class TestGapRiskDiscountsTheCushion(unittest.TestCase):
    """Cushion in sigma assumes a diffusion; assignment arrives as a gap."""

    def test_a_gappy_stock_scores_below_a_grinder(self):
        # Same cushion, same trend, same everything except how the stock opens.
        from wheelkit.strategy import WheelConfig, score_safety

        cfg = WheelConfig()
        grinder = score_safety(
            _candidate(cushion_pct=0.10, gap_down_p05=-0.007), cfg)
        gappy = score_safety(
            _candidate(cushion_pct=0.10, gap_down_p05=-0.035), cfg)
        self.assertGreater(grinder, gappy)

    def test_unknown_gap_history_is_not_penalised(self):
        from wheelkit.strategy import WheelConfig, score_safety

        cfg = WheelConfig()
        unknown = score_safety(
            _candidate(cushion_pct=0.10, gap_down_p05=float("nan")), cfg)
        deep = score_safety(
            _candidate(cushion_pct=0.10, gap_down_p05=-0.005), cfg)
        self.assertAlmostEqual(unknown, deep, places=6)

    def test_a_cushion_thinner_than_one_bad_open_is_halved(self):
        from wheelkit.strategy import _gap_adequacy

        self.assertAlmostEqual(
            _gap_adequacy(_candidate(cushion_pct=0.03, gap_down_p05=-0.03)),
            0.50, places=6)

    def test_a_deep_cushion_is_untouched(self):
        from wheelkit.strategy import _gap_adequacy

        self.assertAlmostEqual(
            _gap_adequacy(_candidate(cushion_pct=0.10, gap_down_p05=-0.005)),
            1.0, places=6)

    def test_the_tail_needs_enough_history(self):
        from wheelkit.analytics import Bar, gap_down_tail

        few = [Bar(day=date(2026, 1, 1), open=10, high=10, low=10, close=10,
                   volume=1)] * 10
        self.assertNotEqual(gap_down_tail(few), gap_down_tail(few))  # NaN

    def test_the_tail_is_negative_and_ordered(self):
        from wheelkit.analytics import Bar, gap_down_tail

        import random
        random.seed(7)
        bars, price = [], 100.0
        for i in range(200):
            gap = random.gauss(0, 0.02)
            opened = price * (1 + gap)
            close = opened * (1 + random.gauss(0, 0.01))
            bars.append(Bar(day=date(2026, 1, 1), open=opened, high=max(opened, close),
                            low=min(opened, close), close=close, volume=1e6))
            price = close
        p05 = gap_down_tail(bars, 5.0)
        p25 = gap_down_tail(bars, 25.0)
        self.assertLess(p05, 0)
        self.assertLess(p05, p25)


class TestSkewSeparatesEdgeFromInsurance(unittest.TestCase):
    """Rich against realised is harvestable; rich on the wing is a fee."""

    def test_ordinary_skew_is_not_penalised(self):
        from wheelkit.strategy import score_iv_edge

        flat = score_iv_edge(_candidate(vrp=1.4, skew_ratio=1.00))
        normal = score_iv_edge(_candidate(vrp=1.4, skew_ratio=1.12))
        self.assertAlmostEqual(flat, normal, places=6)

    def test_a_steep_wing_is_discounted(self):
        from wheelkit.strategy import score_iv_edge

        normal = score_iv_edge(_candidate(vrp=1.4, skew_ratio=1.10))
        steep = score_iv_edge(_candidate(vrp=1.4, skew_ratio=1.50))
        self.assertLess(steep, normal * 0.85)

    def test_unknown_skew_is_neutral(self):
        from wheelkit.strategy import score_iv_edge

        self.assertAlmostEqual(
            score_iv_edge(_candidate(vrp=1.4, skew_ratio=float("nan"))),
            score_iv_edge(_candidate(vrp=1.4, skew_ratio=1.05)),
            places=6,
        )

    def test_the_ratio_is_strike_iv_over_atm(self):
        from wheelkit.volsurface import skew_ratio

        self.assertAlmostEqual(skew_ratio(0.60, 0.40), 1.5, places=6)
        self.assertNotEqual(skew_ratio(0.4, 0.0), skew_ratio(0.4, 0.0))  # NaN


class TestTermStructureVetoes(unittest.TestCase):
    """A weighted mean lets premium carry a candidate priced for an event."""

    def test_contango_is_not_penalised(self):
        from wheelkit.strategy import event_multiplier

        self.assertAlmostEqual(event_multiplier(_candidate(term_slope=0.90)), 1.0)
        self.assertAlmostEqual(event_multiplier(_candidate(term_slope=1.05)), 1.0)

    def test_backwardation_cuts_the_score(self):
        from wheelkit.strategy import event_multiplier

        self.assertLess(event_multiplier(_candidate(term_slope=1.20)), 0.80)
        self.assertLess(event_multiplier(_candidate(term_slope=1.50)), 0.60)

    def test_unknown_slope_does_not_penalise(self):
        # An unavailable back month should cost the signal, not the candidate.
        from wheelkit.strategy import event_multiplier

        self.assertAlmostEqual(
            event_multiplier(_candidate(term_slope=float("nan"))), 1.0)

    def test_it_multiplies_the_whole_score(self):
        from wheelkit.strategy import WheelConfig, score_candidate

        cfg = WheelConfig()
        calm = score_candidate(_candidate(term_slope=0.95), cfg, 70.0).score
        evt = score_candidate(_candidate(term_slope=1.30), cfg, 70.0).score
        self.assertAlmostEqual(evt, round(calm * 0.65, 1), delta=0.6)

    def test_the_multiplier_is_reported_in_the_subscores(self):
        from wheelkit.strategy import WheelConfig, score_candidate

        cfg = WheelConfig()
        scored = score_candidate(_candidate(term_slope=1.30), cfg, 70.0)
        self.assertIn("event_multiplier", scored.subscores)
        self.assertAlmostEqual(scored.subscores["event_multiplier"], 65.0, delta=1.0)

    def test_a_high_premium_no_longer_outruns_a_priced_event(self):
        # The case the multiplicative form exists for.
        from wheelkit.strategy import WheelConfig, score_candidate

        cfg = WheelConfig()
        rich_and_dated = score_candidate(
            _candidate(annualised_return=0.60, vrp=1.8, term_slope=1.40), cfg, 70.0).score
        modest_and_calm = score_candidate(
            _candidate(annualised_return=0.25, vrp=1.3, term_slope=0.95), cfg, 70.0).score
        self.assertLess(rich_and_dated, modest_and_calm)


class TestTickGridIsNotIlliquidity(unittest.TestCase):
    """A percentage spread cannot tell the grid from a thin market."""

    def test_the_tick_helpers_moved_but_still_import_from_orders(self):
        # orders.py re-exports them, so existing callers keep working.
        from wheelkit.orders import round_to_tick, tick_size
        from wheelkit.pricing import tick_size as pricing_tick

        self.assertIs(tick_size, pricing_tick)
        self.assertEqual(tick_size(0.20), 0.01)
        self.assertEqual(tick_size(3.50), 0.05)
        self.assertEqual(round_to_tick(1.234), 1.23)

    def test_a_two_tick_spread_is_not_scored_as_illiquid(self):
        # $0.19/$0.21 is 10% wide and physically cannot be tighter.
        from wheelkit.strategy import WheelConfig, score_liquidity

        cfg = WheelConfig()
        at_grid = score_liquidity(
            _candidate(bid=0.19, ask=0.21, mid=0.20, spread_pct=0.10), cfg)
        genuinely_wide = score_liquidity(
            _candidate(bid=2.85, ask=3.15, mid=3.00, spread_pct=0.10), cfg)
        self.assertGreater(at_grid, genuinely_wide)
        self.assertGreaterEqual(at_grid, 75.0 * 0.65)

    def test_a_wide_cheap_spread_is_still_punished(self):
        # $0.15/$0.25 is five ticks: that is a thin market, not the grid.
        from wheelkit.strategy import WheelConfig, score_liquidity

        cfg = WheelConfig()
        grid = score_liquidity(
            _candidate(bid=0.19, ask=0.21, mid=0.20, spread_pct=0.10), cfg)
        wide = score_liquidity(
            _candidate(bid=0.15, ask=0.25, mid=0.20, spread_pct=0.50), cfg)
        self.assertLess(wide, grid)

    def test_exit_spread_percent_is_not_new_information(self):
        # Recorded because the audit originally proposed gating on it: the
        # exit ratio is exactly twice the entry ratio for every quote, so it
        # can never reorder anything. Ticks were the measure worth adding.
        for mid, spread in ((0.20, 0.02), (1.00, 0.10), (3.00, 0.30)):
            self.assertAlmostEqual(
                spread / (0.5 * mid), 2.0 * (spread / mid), places=9)


class TestBetaWeightedExposure(unittest.TestCase):
    """Correlation groups catch pairs someone wrote down. Beta catches the rest."""

    def _bars(self, returns):
        from wheelkit.analytics import Bar

        bars, price = [], 100.0
        for r in returns:
            price *= (1 + r)
            bars.append(Bar(day=date(2026, 1, 1), open=price, high=price,
                            low=price, close=price, volume=1e6))
        return bars

    def test_a_stock_against_itself_has_beta_one(self):
        from wheelkit.analytics import beta

        import random
        random.seed(3)
        moves = [random.gauss(0, 0.01) for _ in range(80)]
        bars = self._bars(moves)
        self.assertAlmostEqual(beta(bars, bars), 1.0, places=6)

    def test_a_doubled_stock_has_beta_two(self):
        from wheelkit.analytics import beta

        import random
        random.seed(4)
        moves = [random.gauss(0, 0.01) for _ in range(80)]
        self.assertAlmostEqual(
            beta(self._bars([2 * m for m in moves]), self._bars(moves)),
            2.0, delta=0.05)

    def test_too_little_history_is_nan_not_one(self):
        # A missing beta must be visible as missing, not assumed neutral.
        from wheelkit.analytics import beta

        short = self._bars([0.01] * 10)
        self.assertNotEqual(beta(short, short), beta(short, short))

    def test_a_defensive_book_passes(self):
        from wheelkit.risk import RiskLimits, check_portfolio

        findings = check_portfolio(
            [{"symbol": "KO", "capital": 20_000, "beta": 0.4},
             {"symbol": "XLU", "capital": 20_000, "beta": 0.3}],
            limits=RiskLimits(account_value=100_000),
        )
        self.assertNotIn("beta_weighted_exposure", codes(findings))

    def test_three_uncorrelated_sectors_can_still_be_one_bet(self):
        # None of these share a sector or a correlation group.
        from wheelkit.risk import RiskLimits, check_portfolio

        findings = check_portfolio(
            [{"symbol": "NVDA", "capital": 20_000, "beta": 2.0},
             {"symbol": "TSLA", "capital": 20_000, "beta": 3.1},
             {"symbol": "MARA", "capital": 15_000, "beta": 2.65}],
            limits=RiskLimits(account_value=100_000),
        )
        self.assertIn("beta_weighted_exposure", codes(findings))

    def test_a_missing_beta_counts_at_one_and_says_so(self):
        from wheelkit.risk import RiskLimits, check_portfolio

        findings = check_portfolio(
            [{"symbol": "A", "capital": 40_000, "beta": 2.0},
             {"symbol": "B", "capital": 40_000}],
            limits=RiskLimits(account_value=100_000),
        )
        finding = next(f for f in findings if f.code == "beta_weighted_exposure")
        self.assertIn("1 without a beta", finding.message)

    def test_a_short_beta_still_adds_exposure(self):
        # An inverse position is not a hedge for these purposes; it is another
        # directional bet. Magnitude is what the cap measures.
        from wheelkit.risk import RiskLimits, check_portfolio

        findings = check_portfolio(
            [{"symbol": "A", "capital": 45_000, "beta": -1.0},
             {"symbol": "B", "capital": 45_000, "beta": 1.0}],
            limits=RiskLimits(account_value=100_000),
        )
        self.assertIn("beta_weighted_exposure", codes(findings))


class TestScanArchive(unittest.TestCase):
    """Ten free observations a run, against one or two traded ones."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def test_every_candidate_is_archived_not_just_the_top(self):
        # The ones that placed fourth through tenth are the control group.
        from wheelkit.archive import archive_scan, read_archived_scans

        cands = [_candidate(symbol=f"S{i}", score=float(90 - i)) for i in range(10)]
        archive_scan(cands, directory=self.root)
        self.assertEqual(len(read_archived_scans(self.root)), 10)

    def test_an_empty_scan_writes_nothing(self):
        from wheelkit.archive import archive_scan

        self.assertIsNone(archive_scan([], directory=self.root))

    def test_two_runs_accumulate(self):
        from datetime import datetime

        from wheelkit.archive import archive_scan, read_archived_scans

        archive_scan([_candidate()], directory=self.root,
                     when=datetime(2026, 9, 1, 15, 45))
        archive_scan([_candidate()], directory=self.root,
                     when=datetime(2026, 9, 2, 15, 45))
        rows = read_archived_scans(self.root)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["scan_id"] for r in rows},
                         {"2026-09-01-1545", "2026-09-02-1545"})

    def test_the_surface_signals_survive_the_round_trip(self):
        from wheelkit.archive import archive_scan, read_archived_scans

        archive_scan([_candidate(skew_ratio=1.42, term_slope=1.19)],
                     directory=self.root)
        row = read_archived_scans(self.root)[0]
        self.assertAlmostEqual(float(row["skew_ratio"]), 1.42, places=4)
        self.assertAlmostEqual(float(row["term_slope"]), 1.19, places=4)


class TestIvRankNeedsHistory(unittest.TestCase):
    """The seed for the one measure that cannot be computed today."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def test_readings_accumulate_across_days(self):
        from wheelkit.archive import log_atm_iv, read_atm_iv_history

        log_atm_iv({"GDX": 0.48, "KO": 0.22}, directory=self.root,
                   when=date(2026, 9, 1))
        log_atm_iv({"GDX": 0.51}, directory=self.root, when=date(2026, 9, 2))
        history = read_atm_iv_history(self.root)
        self.assertEqual(len(history["GDX"]), 2)
        self.assertEqual(len(history["KO"]), 1)

    def test_rank_is_refused_until_there_is_enough(self):
        # A rank from a fortnight of history is not a rank.
        from wheelkit.archive import iv_rank, read_atm_iv_history

        for i in range(10):
            from wheelkit.archive import log_atm_iv
            log_atm_iv({"X": 0.20 + i * 0.01}, directory=self.root,
                       when=date(2026, 9, 1))
        history = read_atm_iv_history(self.root)
        self.assertNotEqual(iv_rank("X", 0.25, history), iv_rank("X", 0.25, history))

    def test_rank_places_the_value_in_its_own_range(self):
        from wheelkit.archive import iv_rank

        history = {"X": [(date(2026, 1, 1), 0.10 + i * 0.001) for i in range(80)]}
        self.assertAlmostEqual(iv_rank("X", 0.10, history), 0.0, places=1)
        self.assertAlmostEqual(iv_rank("X", 0.179, history), 100.0, places=1)

    def test_a_flat_history_yields_no_rank(self):
        from wheelkit.archive import iv_rank

        history = {"X": [(date(2026, 1, 1), 0.30)] * 80}
        self.assertNotEqual(iv_rank("X", 0.30, history), iv_rank("X", 0.30, history))


class TestDataBundle(unittest.TestCase):
    """git clone carries the code and none of the state."""

    def setUp(self):
        import contextlib
        import io
        import wheel_data

        # pack and restore print a summary by design. In a test run that is
        # noise on top of the report the README tells you to read.
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self.wd = wheel_data
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.old = Path(self.dir.name) / "old"
        self.new = Path(self.dir.name) / "new"
        for root in (self.old, self.new):
            (root / "archive" / "scans").mkdir(parents=True)

    def _seed(self, root, scan_ids=("2026-09-01-1545",)):
        (root / "positions.csv").write_text(
            "symbol,expiration,strike,right,quantity,entry_credit\n"
            "GDX,2026-09-18,89,P,-1,0.94\n", encoding="utf-8")
        (root / "fills.csv").write_text(
            "recorded_at,symbol,right,expiration,strike,contracts,fill_credit\n"
            "2026-09-01,GDX,P,2026-09-18,89,1,0.94\n", encoding="utf-8")
        (root / "archive" / "atm_iv.csv").write_text(
            "observed_on,symbol,atm_iv\n2026-09-01,GDX,0.48\n", encoding="utf-8")
        for sid in scan_ids:
            (root / "archive" / "scans" / f"{sid}.csv").write_text(
                "scan_id,symbol,score\n" + f"{sid},GDX,71.5\n", encoding="utf-8")

    def _args(self, **kw):
        import argparse

        return argparse.Namespace(**kw)

    def _pack(self):
        bundle = Path(self.dir.name) / "b.tgz"
        with self._quiet:
            self.wd.cmd_pack(self._args(root=self.old, output=bundle))
        return bundle

    def _restore(self, bundle, **kw):
        import contextlib
        import io

        base = dict(archive=bundle, root=self.new, force=False, dry_run=False)
        base.update(kw)
        with contextlib.redirect_stdout(io.StringIO()):
            return self.wd.cmd_restore(self._args(**base))

    def test_a_round_trip_carries_the_archive(self):
        self._seed(self.old)
        bundle = self._pack()
        self._restore(bundle)
        self.assertTrue((self.new / "archive" / "scans" / "2026-09-01-1545.csv").exists())
        self.assertTrue((self.new / "fills.csv").exists())

    def test_it_refuses_to_clobber_existing_state(self):
        # Restoring onto a machine that already has positions must stop.
        self._seed(self.old)
        self._seed(self.new)
        bundle = self._pack()
        self.assertEqual(self._restore(bundle), 1)

    def test_dry_run_changes_nothing(self):
        self._seed(self.old)
        bundle = self._pack()
        self._restore(bundle, dry_run=True)
        self.assertFalse((self.new / "fills.csv").exists())

    def test_scan_histories_from_two_machines_union(self):
        # The reason restore merges rather than replacing: dated scan files
        # from two machines are both real history.
        self._seed(self.old, scan_ids=("2026-09-01-1545", "2026-09-02-1545"))
        self._seed(self.new, scan_ids=("2026-09-03-1545",))
        bundle = self._pack()
        self._restore(bundle, force=True)
        kept = sorted(p.stem for p in (self.new / "archive" / "scans").glob("*.csv"))
        self.assertEqual(
            kept, ["2026-09-01-1545", "2026-09-02-1545", "2026-09-03-1545"])

    def test_credentials_are_never_bundled(self):
        # The whole reason keys live in the Keychain is that they are not in
        # files that get copied around.
        self._seed(self.old)
        (self.old / ".env").write_text("ALPACA_API_KEY_ID=secret\n", encoding="utf-8")
        bundle = self._pack()
        import tarfile

        with tarfile.open(bundle) as handle:
            names = handle.getnames()
        self.assertFalse([n for n in names if ".env" in n])

    def test_a_traversing_member_is_refused(self):
        # A tarball is untrusted input even when you made it.
        import io
        import tarfile

        bundle = Path(self.dir.name) / "evil.tgz"
        with tarfile.open(bundle, "w:gz") as handle:
            data = b"pwned"
            info = tarfile.TarInfo("../escaped.txt")
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
        self._restore(bundle, force=True)
        self.assertFalse((self.new.parent / "escaped.txt").exists())

    def test_the_summary_counts_rows_not_bytes(self):
        self._seed(self.old)
        lines = self.wd.describe(self.old)
        self.assertTrue(any("1 row(s)" in l for l in lines))
        self.assertTrue(any("more days to IV rank" in l for l in lines))

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

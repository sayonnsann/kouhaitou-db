import logging
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from sources import split_adjustments  # noqa: E402


class SplitAdjustmentsTest(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(f"test-splits-{id(self)}")
        self.logger.addHandler(logging.NullHandler())
        self.cfg = {
            "split_price_validation_tolerance": 0.20,
            "split_feed_transition_tolerance": 0.15,
        }

    @staticmethod
    def state(ratio=4.0, active=True):
        return {
            "version": 1,
            "adjustments": [
                {
                    "code": "8309",
                    "execution_date": "2026-07-30",
                    "ratio": ratio,
                    "applied_date": "2026-07-30",
                    "feed_dividend_before": 185.0,
                    "active": active,
                }
            ],
            "feed_snapshots": {
                "8309": {"dividend": 185.0, "observed_date": "2026-07-30"}
            },
        }

    def test_applies_annual_and_monthly_amounts(self):
        stock_div = {
            "8309": {
                "dividend": 185.0,
                "monthly": [0, 92.5, 0, 92.5],
                "count": 2,
            }
        }
        adjusted, state = split_adjustments.apply_to_dividends(
            stock_div,
            self.state(),
            self.cfg,
            self.logger,
            observed_date="2026-07-30",
        )
        self.assertEqual(46.25, adjusted["8309"]["dividend"])
        self.assertEqual([0.0, 23.125, 0.0, 23.125], adjusted["8309"]["monthly"])
        self.assertEqual(185.0, stock_div["8309"]["dividend"])
        self.assertTrue(state["adjustments"][0]["active"])

    def test_rejects_event_when_price_change_is_inconsistent(self):
        change = {
            "previous_date": "2026-07-29",
            "latest_date": "2026-07-30",
            "previous_close": 1000.0,
            "latest_close": 500.0,
            "price_ratio": 0.5,
        }

        def fetcher(code, start, end, logger):
            return [{"execution_date": "2026-07-30", "ratio": 4.0}]

        with self.assertRaises(split_adjustments.SplitAdjustmentError):
            split_adjustments.detect_and_record(
                {"9999": change},
                {"9999": {"dividend": 100.0}},
                split_adjustments.empty_state(),
                self.cfg,
                self.logger,
                event_fetcher=fetcher,
                applied_date="2026-07-30",
            )

    def test_repeated_build_does_not_apply_factor_twice(self):
        raw = {"8309": {"dividend": 185.0, "count": 2}}
        first, state = split_adjustments.apply_to_dividends(
            raw, self.state(), self.cfg, self.logger, observed_date="2026-07-30"
        )
        second, state = split_adjustments.apply_to_dividends(
            raw, state, self.cfg, self.logger, observed_date="2026-07-31"
        )
        self.assertEqual(46.25, first["8309"]["dividend"])
        self.assertEqual(46.25, second["8309"]["dividend"])
        self.assertEqual(185.0, raw["8309"]["dividend"])
        self.assertEqual(1, len(state["adjustments"]))

    def test_reverse_split_ratio_below_one(self):
        state = self.state(ratio=0.2)
        state["feed_snapshots"]["8309"]["dividend"] = 10.0
        raw = {"8309": {"dividend": 10.0}}
        adjusted, _ = split_adjustments.apply_to_dividends(
            raw, state, self.cfg, self.logger, observed_date="2026-07-30"
        )
        self.assertEqual(50.0, adjusted["8309"]["dividend"])

    def test_deactivates_factor_when_feed_becomes_adjusted(self):
        first, state = split_adjustments.apply_to_dividends(
            {"8309": {"dividend": 185.0}},
            self.state(),
            self.cfg,
            self.logger,
            observed_date="2026-07-30",
        )
        second, state = split_adjustments.apply_to_dividends(
            {"8309": {"dividend": 46.25}},
            state,
            self.cfg,
            self.logger,
            observed_date="2027-06-30",
        )
        self.assertEqual(46.25, first["8309"]["dividend"])
        self.assertEqual(46.25, second["8309"]["dividend"])
        self.assertFalse(state["adjustments"][0]["active"])
        self.assertEqual("2027-06-30", state["adjustments"][0]["feed_adjusted_date"])

    def test_valid_event_is_recorded_once_and_persists(self):
        change = {
            "previous_date": "2026-07-29",
            "latest_date": "2026-07-30",
            "previous_close": 6800.0,
            "latest_close": 1710.0,
            "price_ratio": 1710.0 / 6800.0,
        }

        def fetcher(code, start, end, logger):
            return [{"execution_date": "2026-07-30", "ratio": 4.0}]

        state = split_adjustments.detect_and_record(
            {"8309": change},
            {"8309": {"dividend": 185.0}},
            split_adjustments.empty_state(),
            self.cfg,
            self.logger,
            event_fetcher=fetcher,
            applied_date="2026-07-30",
        )
        state = split_adjustments.detect_and_record(
            {"8309": change},
            {"8309": {"dividend": 185.0}},
            state,
            self.cfg,
            self.logger,
            event_fetcher=fetcher,
            applied_date="2026-07-30",
        )
        self.assertEqual(1, len(state["adjustments"]))

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "split_adjustments.json")
            split_adjustments.save_state(path, state)
            self.assertEqual(state, split_adjustments.load_state(path))

    def test_duplicate_persistent_record_is_rejected(self):
        state = self.state()
        state["adjustments"].append(dict(state["adjustments"][0]))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "split_adjustments.json")
            split_adjustments.save_state(path, state)
            with self.assertRaises(split_adjustments.SplitAdjustmentError):
                split_adjustments.load_state(path)


if __name__ == "__main__":
    unittest.main()

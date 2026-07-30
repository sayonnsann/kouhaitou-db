import logging
import os
import sys
import unittest

import pandas as pd


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from sources import prices  # noqa: E402


class PriceSplitCandidatesTest(unittest.TestCase):
    def test_only_large_price_change_becomes_event_query_candidate(self):
        index = pd.to_datetime(["2026-07-29", "2026-07-30"])
        data = pd.DataFrame(
            [
                [6800.0, 1000.0],
                [1710.0, 1050.0],
            ],
            index=index,
            columns=pd.MultiIndex.from_tuples(
                [("Close", "8309.T"), ("Close", "8058.T")]
            ),
        )

        class FakeYfinance:
            @staticmethod
            def download(*args, **kwargs):
                return data

        original = prices.yf
        prices.yf = FakeYfinance()
        try:
            result, changes = prices.get_prices_with_changes(
                ["8309", "8058"],
                {
                    "yfinance_chunk_size": 250,
                    "split_candidate_min_price_ratio": 2.0 / 3.0,
                    "split_candidate_max_price_ratio": 1.5,
                },
                logging.getLogger("test-price-candidates"),
            )
        finally:
            prices.yf = original

        self.assertEqual({"8309": 1710.0, "8058": 1050.0}, result)
        self.assertEqual(["8309"], list(changes))
        self.assertEqual("2026-07-30", changes["8309"]["latest_date"])

    def test_persisted_price_detects_split_when_history_is_retroactively_adjusted(self):
        index = pd.to_datetime(["2026-07-29", "2026-07-30"])
        data = pd.DataFrame(
            [[1700.0], [1710.0]],
            index=index,
            columns=pd.MultiIndex.from_tuples([("Close", "8309.T")]),
        )

        class FakeYfinance:
            @staticmethod
            def download(*args, **kwargs):
                return data

        original = prices.yf
        prices.yf = FakeYfinance()
        try:
            _, changes = prices.get_prices_with_changes(
                ["8309"],
                {},
                logging.getLogger("test-persisted-price"),
                previous_prices={"8309": 6800.0},
                previous_date="2026-07-29",
            )
        finally:
            prices.yf = original

        self.assertEqual(["8309"], list(changes))
        self.assertEqual(6800.0, changes["8309"]["previous_close"])
        self.assertAlmostEqual(1710.0 / 6800.0, changes["8309"]["price_ratio"])


if __name__ == "__main__":
    unittest.main()

import logging
import os
import sys
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import build_database  # noqa: E402
from sources import split_adjustments  # noqa: E402


class BuildSplitIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(f"test-build-splits-{id(self)}")
        self.logger.addHandler(logging.NullHandler())
        self.cfg = {
            "month_basis": "payment",
            "value_mode": "amount",
            "split_feed_transition_tolerance": 0.15,
        }
        self.state = split_adjustments.load_state(
            os.path.join(REPO_ROOT, "data", "split_adjustments.json")
        )

    def test_8309_build_has_19_columns_and_split_adjusted_yield(self):
        # 6812円の分割前終値を1:4で理論調整した1703円を使用する。
        universe = [
            {
                "code": "8309",
                "name": "三井住友トラストグループ",
                "market": "プライム（内国株式）",
                "sector33": "銀行業",
                "instrument": "stock",
            }
        ]
        raw = {
            "8309": {
                "dividend": 185.0,
                "count": 2,
                "record_months": [3, 9],
                "year": "2025",
            }
        }
        adjusted, _ = split_adjustments.apply_to_dividends(
            raw,
            self.state,
            self.cfg,
            self.logger,
            observed_date="2026-07-30",
        )
        rows = build_database.build_rows(
            universe,
            {"8309": 1703.0},
            adjusted,
            {},
            self.cfg,
            self.logger,
        )
        row = rows[0]
        self.assertEqual(19, len(row))
        self.assertEqual("46.25", row[4])
        self.assertEqual("23.125", row[11])  # 6月
        self.assertEqual("23.125", row[17])  # 12月
        self.assertAlmostEqual(185.0 / 6812.0, float(row[4]) / float(row[18]))

    def test_non_split_stock_is_byte_for_byte_unchanged(self):
        raw = {
            "8309": {
                "dividend": 185.0,
                "count": 2,
                "record_months": [3, 9],
            },
            "8058": {
                "dividend": 110.0,
                "count": 2,
                "record_months": [3, 9],
                "year": "2025",
                "source": "edinet",
            },
        }
        adjusted, _ = split_adjustments.apply_to_dividends(
            raw,
            self.state,
            self.cfg,
            self.logger,
            observed_date="2026-07-30",
        )
        self.assertEqual(raw["8058"], adjusted["8058"])


if __name__ == "__main__":
    unittest.main()

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import update_prices_only as upo  # noqa: E402
from sources import prices  # noqa: E402

JST = timezone(timedelta(hours=9))

HEADER = [
    "銘柄コード", "銘柄名", "市場区分", "業種", "年間配当金", "年間配当回数",
    "1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月",
    "2026/08/12 06:00:00",
]

ROWS = [
    ["1301", "極洋", "プライム", "水産", "150", "1",
     "0", "0", "0", "0", "0", "150", "0", "0", "0", "0", "0", "0", "4600"],
    ["1305", "ｉＦｒｅｅＥＴＦ", "ETF", "ETF", "79.4", "1",
     "0", "0", "0", "0", "0", "0", "79.4", "0", "0", "0", "0", "0", "4300"],
    ["9999", "未寄付銘柄", "プライム", "その他", "10", "1",
     "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "10", "0", "1000"],
]


class UpdatePricesOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(data_dir)
        self.db_path = os.path.join(data_dir, "database.csv")
        self.meta_path = os.path.join(data_dir, "price_update_meta.json")
        with open(self.db_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(ROWS)

        # scripts/update_prices_only.py の _abspath をテスト用一時ディレクトリに差し替える
        self._orig_abspath = upo._abspath
        upo._abspath = lambda rel: os.path.join(self.tmpdir, rel)

        self._orig_yf = prices.yf

    def tearDown(self):
        upo._abspath = self._orig_abspath
        prices.yf = self._orig_yf
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_csv(self):
        with open(self.db_path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        return rows[0], {r[0]: r for r in rows[1:]}

    def test_morning_open_updates_only_todays_open_and_preserves_other_columns(self):
        today = datetime.now(JST).date().isoformat()
        yesterday = (datetime.now(JST).date() - timedelta(days=1)).isoformat()
        index = pd.to_datetime([yesterday, today])

        data = pd.DataFrame(
            {
                ("Open", "1301.T"): [4550.0, 4700.0],   # 当日分あり
                ("Open", "1305.T"): [4300.0, None],      # 当日分なし(未寄り付き)
                ("Open", "9999.T"): [990.0, None],       # 当日分なし
            },
            index=index,
        )
        data.columns = pd.MultiIndex.from_tuples(data.columns)

        class FakeYF:
            @staticmethod
            def download(*a, **kw):
                return data

        prices.yf = FakeYF()
        upo.main(["--session", "morning_open"])

        header, by_code = self._read_csv()

        self.assertEqual(by_code["1301"][18], "4700")
        # B〜R列は変更されていないこと
        self.assertEqual(by_code["1301"][1], "極洋")
        self.assertEqual(by_code["1301"][6], "0")
        self.assertEqual(by_code["1301"][11], "150")

        # 当日分が無い銘柄は前回値を保持
        self.assertEqual(by_code["1305"][18], "4300")
        self.assertEqual(by_code["9999"][18], "1000")

        self.assertNotEqual(header[18], "2026/08/12 06:00:00")

        with open(self.meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["session"], "morning_open")
        self.assertEqual(meta["session_label"], "前場寄付")
        self.assertEqual(meta["updated_count"], 1)
        self.assertEqual(meta["total_count"], 3)

    def test_afternoon_close_updates_available_codes(self):
        today = datetime.now(JST).date().isoformat()
        index = pd.to_datetime([today])
        data = pd.DataFrame(
            {
                ("Close", "1301.T"): [4650.0],
                ("Close", "1305.T"): [4310.0],
            },
            index=index,
        )
        data.columns = pd.MultiIndex.from_tuples(data.columns)

        class FakeYF:
            @staticmethod
            def download(*a, **kw):
                return data

        prices.yf = FakeYF()
        upo.main(["--session", "afternoon_close"])

        _, by_code = self._read_csv()
        self.assertEqual(by_code["1301"][18], "4650")
        self.assertEqual(by_code["1305"][18], "4310")
        # 取得できなかった銘柄は前回値保持
        self.assertEqual(by_code["9999"][18], "1000")

        with open(self.meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["session"], "afternoon_close")
        self.assertEqual(meta["session_label"], "後場引け")

    def test_total_failure_leaves_csv_and_meta_untouched(self):
        class FakeYF:
            @staticmethod
            def download(*a, **kw):
                raise RuntimeError("network down")

        prices.yf = FakeYF()
        with open(self.db_path, encoding="utf-8") as f:
            before = f.read()

        upo.main(["--session", "afternoon_close"])  # 例外を投げず終了すること

        with open(self.db_path, encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(self.meta_path))

    def test_missing_database_csv_exits_nonzero(self):
        os.remove(self.db_path)
        class FakeYF:
            @staticmethod
            def download(*a, **kw):
                return None

        prices.yf = FakeYF()
        with self.assertRaises(SystemExit) as ctx:
            upo.main(["--session", "morning_open"])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()

# 役割: 全モジュール共通のユーティリティ群
# - 設定ファイル(config.yaml)のロード
# - HTTPセッション生成(User-Agent付き)
# - ロガー生成(標準出力へ)
# - JST(日本標準時)のタイムスタンプ生成
# - 年間配当を各月に割り付ける build_monthly()（月次配当列 G〜R の生成ロジック）

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
import yaml

# JST(UTC+9)のタイムゾーンオブジェクト
JST = timezone(timedelta(hours=9))


def load_config(config_path=None):
    """config/config.yaml を読み込んで dict で返す。

    config_path 未指定時は、このファイルからの相対で
    ../config/config.yaml を探す。
    """
    if config_path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(here, "..", "config", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg or {}


def get_logger(name="kouhaitou"):
    """標準出力へINFOレベルで吐くロガーを返す（GitHub Actionsのログ用）。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def build_session(cfg):
    """config の user_agent を付与した requests.Session を返す。

    IR BANK / JPX へのアクセスで共通利用する。ブラウザ風UAが必要な場面が
    あるため、UAは config から差し込む。
    """
    session = requests.Session()
    ua = cfg.get("user_agent", "kouhaitou-db/1.0")
    session.headers.update({"User-Agent": ua})
    return session


def jst_timestamp():
    """現在時刻をJSTの文字列で返す（データベースS1セル=最終更新日時 用）。"""
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _payment_months_from_record(record_months, month_basis):
    """権利確定月リストから、実際に配当額を置く月リストを返す。

    - month_basis == "record": 権利確定月そのもの
    - month_basis == "payment"(既定): 権利確定月 + 3ヶ月（日本の慣行）
      例) 3月末権利 → 6月支払、9月末権利 → 12月支払
    """
    if month_basis == "record":
        return [m for m in record_months]
    # payment: +3ヶ月シフト（12を超えたら翌年に回り込む）
    shifted = []
    for m in record_months:
        pm = m + 3
        if pm > 12:
            pm -= 12
        shifted.append(pm)
    return shifted


def _infer_record_months(year_end_month, count):
    """決算月(=年度末権利確定月)と回数から、権利確定月のリストを推定する。

    - 1回: 決算月のみ
    - 2回: 決算月 と その6ヶ月前（中間）
    - 4回: 決算月から3ヶ月刻みで4つ
    - その他: 均等割り（12/回数 刻み）
    """
    def wrap(m):
        # 1〜12 に丸め込む
        m = ((m - 1) % 12) + 1
        return m

    if count <= 1:
        return [wrap(year_end_month)]
    if count == 2:
        return sorted([wrap(year_end_month), wrap(year_end_month - 6)])
    if count == 4:
        return sorted([wrap(year_end_month - 3 * i) for i in range(4)])
    # 想定外の回数は均等割り
    step = max(1, round(12 / count))
    return sorted([wrap(year_end_month - step * i) for i in range(count)])


def build_monthly(annual, count, record_months, month_basis="payment", value_mode="amount"):
    """年間配当を12ヶ月(1月〜12月, G〜R列)に割り付けたリスト[float]*12 を返す。

    引数:
        annual: 年間配当額(円/株)
        count : 年間配当回数
        record_months: 権利確定月のリスト(例 [3, 9])。空なら count から推定できない。
        month_basis: "payment"(既定, 権利確定+3ヶ月) / "record"(権利確定月そのもの)
        value_mode : "amount"(既定, 各月に金額) / "flag"(支払月に1, それ以外0)

    戻り値: 長さ12のリスト。index0=1月 ... index11=12月。
    """
    months = [0.0] * 12

    # annual が無効なら全ゼロを返す（配当情報が取れなかった銘柄）
    try:
        annual = float(annual)
    except (TypeError, ValueError):
        return months
    if annual <= 0:
        return months

    # 回数の正規化
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        count = len(record_months) if record_months else 1

    # 権利確定月が未提供なら推定できないため、回数だけでは月が定まらない。
    # その場合は record_months が無ければ何もしない（金額を宙に浮かせない）。
    rec = list(record_months) if record_months else []
    if not rec:
        # 決算月情報が無いケース。安全側で年度末=3月と仮定せず、空のまま返す。
        return months

    # 実際に金額を置く月
    pay_months = _payment_months_from_record(rec, month_basis)

    # 各支払月への金額割り当て。
    # record_months に各期の金額が畳み込まれていない(=月だけ)ため、
    # ここでは annual を支払回数で均等割りする。
    n = len(pay_months)
    if n == 0:
        return months
    per = annual / n

    for pm in pay_months:
        idx = pm - 1
        if 0 <= idx < 12:
            if value_mode == "flag":
                months[idx] = 1.0
            else:
                months[idx] += per

    return months

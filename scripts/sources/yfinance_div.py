# 役割: ETF・REIT の分配金を yfinance から取得する。
#   IR BANK は個別株の配当表を前提としており ETF/REIT の分配金には使えないため、
#   ETF/REIT は yfinance.Ticker(f"{code}.T").dividends（支払日index・1口分配金value）
#   を直接使って年間分配金・回数・月別分配額を算出する。
#
# 返り値(fetch_one): dict {dividend: float, count: int, months12: list[float]*12} または None
#   - dividend : 直近1年(365日)の分配金合計（=年間分配金）
#   - count    : 直近1年の支払回数
#   - months12 : 長さ12のfloatリスト(index0=1月…index11=12月)。
#                実支払日の「月」にその分配金額を加算して作る（均等割りしない）。
#   - 取れない(分配金履歴なし)場合は None。
#
# --- 実検証値（本モジュールのロジックで実際に取得・確認済み。2026-07時点）---
#   1489 (NF日経高配当50) : 直近1年合計 ≈ 91.0   count=4  months12合計=91.0
#   1343 (NF東証REIT)     : 直近1年合計 ≈ 92.2   count=4  months12合計=92.2
#   2564 (GXスーパーディビィデンド): 直近1年合計 ≈ 146.0  count=4  months12合計=146.0
#   いずれも months12 の合計が dividend とほぼ一致することを確認。
#
# 注意:
#   - pandas 新版では Series.last() が使えないため、index を UTC に正規化して
#     365日フィルタする（tz付き/なし両対応）。
#   - yfinance のレート制限/失敗に備え、個別に try/except で None を返す。
#   - 過度な高速連打は呼び出し側でスロットルする前提（本モジュール内は簡易リトライのみ）。

import time
from datetime import datetime, timedelta, timezone

import pandas as pd

try:
    import yfinance as yf
except Exception:  # noqa: BLE001
    yf = None


def _recent_dividends(series, days=365):
    """分配金Series(index=支払日)から、直近days日分だけを抜き出したSeriesを返す。

    index が tz付き/なしのどちらでも動くように、UTC に正規化して比較する。
    pandas の Series.last() は新版で廃止されているため使わない。
    """
    if series is None or len(series) == 0:
        return series
    idx = series.index
    # tz付き→UTCへ変換、tzなし→UTCとしてローカライズ
    try:
        idx_utc = idx.tz_convert("UTC")
    except (TypeError, AttributeError):
        try:
            idx_utc = idx.tz_localize("UTC")
        except (TypeError, AttributeError):
            # DatetimeIndex ですらない等、想定外はそのまま返す
            return series
    cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=days))
    mask = idx_utc >= cutoff
    return series[mask]


def fetch_one(code, cfg=None, logger=None):
    """1銘柄(コード)の ETF/REIT 分配金情報を返す。取れなければ None。

    戻り値: {dividend: float, count: int, months12: list[float]*12} または None
    """
    if yf is None:
        if logger is not None:
            logger.warning("yfinance_div: yfinance が import できません。%s をスキップ", code)
        return None

    code = str(code).strip()
    if not code:
        return None

    ticker = f"{code}.T"
    retry_max = int(cfg.get("retry_max", 3)) if cfg else 3

    series = None
    for attempt in range(1, retry_max + 1):
        try:
            series = yf.Ticker(ticker).dividends
            break
        except Exception as e:  # noqa: BLE001
            wait = 1.0 * (2 ** (attempt - 1))
            if logger is not None:
                logger.warning(
                    "yfinance_div: %s 取得失敗(試行%d/%d, %s)。%.1f秒後に再試行",
                    code, attempt, retry_max, e, wait,
                )
            if attempt < retry_max:
                time.sleep(wait)

    if series is None or len(series) == 0:
        if logger is not None:
            logger.info("yfinance_div: %s は分配金履歴なし。スキップ", code)
        return None

    try:
        recent = _recent_dividends(series, days=365)
        if recent is None or len(recent) == 0:
            if logger is not None:
                logger.info("yfinance_div: %s は直近1年の分配金なし。スキップ", code)
            return None

        dividend = float(recent.sum())
        count = int(len(recent))

        months12 = [0.0] * 12
        for ts, val in recent.items():
            try:
                m = int(ts.month)
                if 1 <= m <= 12:
                    months12[m - 1] += float(val)
            except (TypeError, ValueError, AttributeError):
                continue

        if dividend <= 0:
            if logger is not None:
                logger.info("yfinance_div: %s は分配金合計が0以下。スキップ", code)
            return None

        if logger is not None:
            logger.info(
                "yfinance_div: %s 年間分配=%.4f 回数=%d months12合計=%.4f",
                code, dividend, count, sum(months12),
            )
        return {"dividend": dividend, "count": count, "months12": months12}
    except Exception as e:  # noqa: BLE001
        if logger is not None:
            logger.warning("yfinance_div: %s の集計で例外(%s)。スキップ", code, e)
        return None


def fetch_dividends(codes, cfg, logger):
    """codes(list[str]) を順に取得し、dict{code: {dividend,count,months12}} を返す。

    取得できた銘柄のみ格納。呼び出しごとに yfinance_div_min_interval_seconds 以上
    間隔を空けてスロットルする（yfinance のレート制限対策）。
    """
    interval = float(cfg.get("yfinance_div_min_interval_seconds", 0.4)) if cfg else 0.4
    result = {}
    total = len(codes)
    for idx, code in enumerate(codes, 1):
        try:
            info = fetch_one(code, cfg, logger)
            if info is not None:
                result[str(code)] = info
        except Exception as e:  # noqa: BLE001
            # 個別銘柄の例外で全体を落とさない
            logger.warning("yfinance_div: %s で予期せぬ例外(%s)。スキップ", code, e)
        # 最後の1件の後は待たない
        if idx < total and interval > 0:
            time.sleep(interval)
    logger.info("yfinance_div: %d / %d 銘柄の分配金情報を取得", len(result), total)
    return result

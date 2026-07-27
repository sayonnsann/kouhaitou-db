# 役割: 内国株式の配当を、自前のEDINET配信データから読む。
#
# データ元: 公開リポジトリ sayonnsann/pharmacistlife-dividend-data の
#           edinet/{4桁コード}.json（金融庁EDINETの有価証券報告書由来。再配布自由）
#
# 1銘柄JSONのうち、このモジュールが使うキー:
#   dps         : {年: 1株あたり年間配当}  株式分割・併合を最新基準に揃え済み
#   dpsInterim  : {年: 1株あたり中間配当}  dps と同じ基準
#   fiscalMonth : 決算月(1〜12)
#
# 返り値(fetch_one): dict {dividend, count, record_months, source:"edinet"} または None
#   - dividend      : dps の最新年の値（＝直近実績の年間配当）。0以下・欠損なら None を返す
#   - count         : 最新年の中間配当が正なら 2、そうでなければ 1
#   - record_months : 決算月と回数から推定した権利確定月（util._infer_record_months と同一ロジック）
#
# 取得方法:
#   - 既定はローカルのディレクトリ読み（GitHub Actions では actions/checkout で
#     配信リポジトリを2つ目にチェックアウトし、そのパスを渡す）。
#     3,808銘柄ぶんのHTTP取得を発生させないための設計。
#   - ローカルにディレクトリが無いときだけ jsDelivr から1銘柄ずつ取る（動作確認用）。

import json
import os
import time

# ローカルにフィードが無いときのフォールバック（動作確認用。CIでは使わない想定）
JSDELIVR_BASE = (
    "https://cdn.jsdelivr.net/gh/sayonnsann/pharmacistlife-dividend-data@main/edinet"
)


def resolve_feed_dir(cfg=None, explicit=None):
    """使うフィードのディレクトリを返す。見つからなければ None（=jsDelivrへ）。

    優先順: 引数 --feed-dir > 環境変数 EDINET_FEED_DIR > config の edinet_feed_dir
    """
    candidates = [
        explicit,
        os.environ.get("EDINET_FEED_DIR"),
        (cfg or {}).get("edinet_feed_dir"),
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return None


def _infer_record_months(year_end_month, count):
    """決算月(=年度末権利確定月)と回数から権利確定月リストを推定する。

    util._infer_record_months と同一ロジック（このモジュール単独でも動くよう複製）。
    """

    def wrap(m):
        return ((m - 1) % 12) + 1

    if count <= 1:
        return [wrap(year_end_month)]
    if count == 2:
        return sorted([wrap(year_end_month), wrap(year_end_month - 6)])
    if count == 4:
        return sorted([wrap(year_end_month - 3 * i) for i in range(4)])
    step = max(1, round(12 / count))
    return sorted([wrap(year_end_month - step * i) for i in range(count)])


def _load_local(feed_dir, code):
    path = os.path.join(feed_dir, f"{code}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_remote(session, code, cfg, logger):
    """jsDelivr から1銘柄ぶんのJSONを取る。404 は None（未収録）。"""
    url = f"{JSDELIVR_BASE}/{code}.json"
    timeout = int((cfg or {}).get("request_timeout", 20))
    retry_max = int((cfg or {}).get("retry_max", 3))
    for attempt in range(1, retry_max + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            if attempt >= retry_max:
                if logger is not None:
                    logger.warning("edinet_feed: %s の取得に失敗(%s)。スキップ", code, e)
                return None
            time.sleep(1.0 * (2 ** (attempt - 1)))
    return None


def parse_entry(entry, logger=None, code=""):
    """1銘柄JSONから {dividend, count, record_months, source} を作る。使えなければ None。"""
    if not isinstance(entry, dict):
        return None
    dps = entry.get("dps")
    if not isinstance(dps, dict) or not dps:
        return None  # 配当の系列が無い＝E列は空欄（0で埋めない）
    latest = max(dps)
    try:
        dividend = float(dps[latest])
    except (TypeError, ValueError):
        return None
    if dividend <= 0:
        return None  # 無配。空欄にして「取れなかった」と同じ扱いにする

    # 回数: 最新年の中間配当が正なら年2回、そうでなければ年1回。
    interim = entry.get("dpsInterim")
    mid = interim.get(latest) if isinstance(interim, dict) else None
    try:
        count = 2 if (mid is not None and float(mid) > 0) else 1
    except (TypeError, ValueError):
        count = 1

    # 権利確定月: 決算月から推定する（既存ロジック）。決算月が無ければ月別は作れない。
    record_months = []
    month = entry.get("fiscalMonth")
    if isinstance(month, int) and 1 <= month <= 12:
        record_months = _infer_record_months(month, count)

    if logger is not None:
        logger.debug(
            "edinet_feed: %s %s年 年間=%s 中間=%s 回数=%d 決算月=%s 権利月=%s",
            code, latest, dividend, mid, count, month, record_months,
        )
    return {
        "dividend": dividend,
        "count": count,
        "record_months": record_months,
        "year": str(latest),
        "source": "edinet",
    }


def fetch_one(code, feed_dir=None, session=None, cfg=None, logger=None):
    """1銘柄ぶんの配当情報を返す。フィードに無い・配当が無いなら None。"""
    code = str(code).strip()
    if not code:
        return None
    try:
        if feed_dir:
            entry = _load_local(feed_dir, code)
        elif session is not None:
            entry = _load_remote(session, code, cfg, logger)
        else:
            entry = None
    except Exception as e:  # noqa: BLE001
        if logger is not None:
            logger.warning("edinet_feed: %s の読み込みで例外(%s)。スキップ", code, e)
        return None
    return parse_entry(entry, logger, code)


def fetch_dividends(codes, cfg, logger, feed_dir=None, session=None):
    """codes を順に読み、dict{code: {dividend,count,record_months,source}} を返す。

    feed_dir が解決できればローカル読み（HTTPアクセスなし）。
    無い場合だけ jsDelivr から1銘柄ずつ取る。
    """
    feed_dir = resolve_feed_dir(cfg, feed_dir)
    if feed_dir:
        logger.info("edinet_feed: ローカルのフィードを使用: %s", feed_dir)
    else:
        logger.warning(
            "edinet_feed: ローカルのフィードが見つからないため jsDelivr から取得します"
            "（%d 銘柄ぶんのHTTPアクセスが発生します）", len(codes)
        )

    result = {}
    missing = 0
    for code in codes:
        info = fetch_one(code, feed_dir, session, cfg, logger)
        if info is None:
            missing += 1
            continue
        result[str(code)] = info
    logger.info(
        "edinet_feed: %d / %d 銘柄の配当を取得（未収録・無配など %d 件）",
        len(result), len(codes), missing,
    )
    return result

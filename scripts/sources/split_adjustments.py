"""株式分割・併合イベントを検証し、EDINET配当を現在株数基準へ揃える。

全銘柄への個別イベント照会は行わない。prices が一括取得時に見つけた
「前営業日比が 2/3 未満または 1.5 超」の内国株式だけ Yahoo の split event
を照会し、イベント比率で株価変動を戻した値が 1 に近い場合だけ採用する。

EDINET feed は毎日元値から読み直されるため、active な係数を毎回掛け直す。
一方、feed の生値自体が前回値から active 係数ぶん変化したら、feed 側が
調整済みになったと判断して古い係数を無効化する。
"""

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import os

try:
    import yfinance as yf
except Exception:  # noqa: BLE001
    yf = None


STATE_VERSION = 1


class SplitAdjustmentError(RuntimeError):
    """検証できない急変、または永続データ破損を表す致命的エラー。"""


def empty_state():
    return {"version": STATE_VERSION, "adjustments": [], "feed_snapshots": {}}


def _validate_state(state, path="split adjustment state"):
    if (
        not isinstance(state, dict)
        or state.get("version") != STATE_VERSION
        or not isinstance(state.get("adjustments"), list)
        or not isinstance(state.get("feed_snapshots"), dict)
    ):
        raise SplitAdjustmentError(f"分割調整ファイルの形式が不正です: {path}")

    seen = set()
    for item in state["adjustments"]:
        if not isinstance(item, dict):
            raise SplitAdjustmentError(f"分割調整レコードの形式が不正です: {path}")
        code = str(item.get("code", ""))
        execution_date = str(item.get("execution_date", ""))
        applied_date = str(item.get("applied_date", ""))
        try:
            _as_date(execution_date)
            _as_date(applied_date)
            ratio = float(item["ratio"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SplitAdjustmentError(f"分割調整レコードの値が不正です: {path}") from exc
        if not code or ratio <= 0 or ratio == 1:
            raise SplitAdjustmentError(f"分割調整レコードの値が不正です: {path}")
        key = (code, execution_date)
        if key in seen:
            raise SplitAdjustmentError(
                f"分割調整レコードが重複しています: {code} {execution_date}"
            )
        seen.add(key)
    return state


def load_state(path):
    """永続状態を読む。破損を空状態として扱うと二重調整を招くため例外にする。"""
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SplitAdjustmentError(f"分割調整ファイルを読み込めません: {path}: {exc}") from exc
    return _validate_state(state, path)


def save_state(path, state):
    """状態を同一ディレクトリ内で atomic に置換する。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
        raise SplitAdjustmentError(f"分割調整ファイルを保存できません: {path}: {exc}") from exc


def _as_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def fetch_split_events(code, start_date, end_date, logger=None):
    """Yahoo/yfinance から指定期間の split event を取得する。"""
    if yf is None:
        raise SplitAdjustmentError("yfinance を import できないため分割イベントを検証できません")
    start = _as_date(start_date) - timedelta(days=2)
    # yfinance の end は排他的なので翌日まで含める。
    end = _as_date(end_date) + timedelta(days=2)
    try:
        history = yf.Ticker(f"{code}.T").history(
            start=start.isoformat(),
            end=end.isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise SplitAdjustmentError(f"{code}: split event の取得に失敗: {exc}") from exc
    if history is None or history.empty or "Stock Splits" not in history.columns:
        return []

    events = []
    for timestamp, raw_ratio in history["Stock Splits"].dropna().items():
        try:
            ratio = float(raw_ratio)
        except (TypeError, ValueError):
            continue
        if ratio <= 0 or ratio == 1:
            continue
        event_date = timestamp.date().isoformat()
        if _as_date(start_date) <= _as_date(event_date) <= _as_date(end_date):
            events.append({"execution_date": event_date, "ratio": ratio})
    if logger is not None:
        logger.info("splits: %s のイベントを %d 件取得", code, len(events))
    return events


def _close_enough(actual, expected, relative_tolerance):
    if expected == 0:
        return actual == 0
    return abs(actual - expected) / abs(expected) <= relative_tolerance


def validate_price_change(code, change, split_ratio, tolerance=0.20):
    """株価変動が split ratio と整合するか検証する。

    分割後価格/分割前価格は概ね 1/split_ratio。通常の日中変動も含むため
    split_ratio を掛けて 1 に戻した値に20%の余裕を持たせる。
    """
    try:
        observed = float(change["price_ratio"])
        ratio = float(split_ratio)
    except (KeyError, TypeError, ValueError) as exc:
        raise SplitAdjustmentError(f"{code}: 株価変動または分割比率が不正です") from exc
    if observed <= 0 or ratio <= 0 or ratio == 1:
        raise SplitAdjustmentError(
            f"{code}: 株価変動または分割比率が範囲外です "
            f"(price_ratio={observed}, split_ratio={ratio})"
        )
    normalized = observed * ratio
    if not _close_enough(normalized, 1.0, float(tolerance)):
        raise SplitAdjustmentError(
            f"{code}: split event と株価変動が不整合です "
            f"(price_ratio={observed:.6g}, split_ratio={ratio:.6g}, "
            f"normalized={normalized:.6g})"
        )


def _find_adjustment(state, code, execution_date):
    for item in state["adjustments"]:
        if item.get("code") == code and item.get("execution_date") == execution_date:
            return item
    return None


def detect_and_record(
    price_changes,
    stock_div,
    state,
    cfg,
    logger,
    event_fetcher=fetch_split_events,
    applied_date=None,
):
    """急変候補をイベント照合し、すべて検証できた場合だけ状態へ追加する。

    途中に不整合が1件でもあれば staged state を破棄して例外にするため、
    検証できない係数が一部だけ永続化されることはない。
    """
    staged = deepcopy(state)
    tolerance = float(cfg.get("split_price_validation_tolerance", 0.20))
    applied_date = applied_date or datetime.now().astimezone().date().isoformat()

    for code, change in sorted(price_changes.items()):
        latest_date = str(change.get("latest_date", ""))
        known = _find_adjustment(staged, code, latest_date)
        if known is not None:
            validate_price_change(code, change, known.get("ratio"), tolerance)
            logger.info("splits: %s %s は記録済み（株価整合性を再確認）", code, latest_date)
            continue

        try:
            events = event_fetcher(
                code,
                change.get("previous_date"),
                change.get("latest_date"),
                logger,
            )
        except SplitAdjustmentError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SplitAdjustmentError(f"{code}: split event の取得に失敗: {exc}") from exc

        matching = [event for event in events if event.get("execution_date") == latest_date]
        if len(matching) == 0:
            # イベントが無い＝分割ではない（ストップ高・TOB等の正当な急変動）。
            # 調整は行わず、記録だけ残して続行する。
            logger.warning(
                "splits: %s %s は急変動だが分割イベントなし（正当な値動きとみなし調整しない）",
                code, latest_date,
            )
            continue
        if len(matching) > 1:
            raise SplitAdjustmentError(
                f"{code}: 同日に複数の split event があり比率を特定できません "
                f"(date={latest_date}, events={events})"
            )
        event = matching[0]
        try:
            ratio = float(event["ratio"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SplitAdjustmentError(
                f"{code}: split event の比率が不正です: {event}"
            ) from exc
        validate_price_change(code, change, ratio, tolerance)

        raw_dividend = (stock_div.get(code) or {}).get("dividend")
        try:
            raw_dividend = float(raw_dividend)
        except (TypeError, ValueError):
            raw_dividend = None
        staged["adjustments"].append(
            {
                "code": code,
                "execution_date": latest_date,
                "ratio": ratio,
                "applied_date": applied_date,
                "feed_dividend_before": raw_dividend,
                # 配当が無い銘柄には適用対象がない。将来初めてfeedへ現れた値は
                # すでに現在株数基準とみなし、根拠なく古い係数を掛けない。
                "active": raw_dividend is not None,
            }
        )
        if raw_dividend is not None:
            staged["feed_snapshots"][code] = {
                "dividend": raw_dividend,
                "observed_date": applied_date,
            }
        logger.info(
            "splits: %s %s ratio=%s を検証し、配当調整へ追加",
            code,
            latest_date,
            ratio,
        )
    return staged


def _active_for_code(state, code):
    return sorted(
        [
            item
            for item in state["adjustments"]
            if item.get("code") == code and item.get("active", True)
        ],
        key=lambda item: item.get("execution_date", ""),
    )


def _feed_adjusted_prefix(previous, current, active, tolerance):
    """feed が反映したとみなせる、古い順の active イベント数を返す。"""
    product = 1.0
    matched = 0
    for index, item in enumerate(active, start=1):
        product *= float(item["ratio"])
        if _close_enough(current, previous / product, tolerance):
            matched = index
    return matched


def apply_to_dividends(stock_div, state, cfg, logger, observed_date=None):
    """active 係数をEDINETの生値へ毎回適用し、調整済みコピーと状態を返す。"""
    adjusted = deepcopy(stock_div)
    staged = deepcopy(state)
    tolerance = float(cfg.get("split_feed_transition_tolerance", 0.15))
    observed_date = observed_date or datetime.now().astimezone().date().isoformat()

    codes = {item.get("code") for item in staged["adjustments"] if item.get("code")}
    for code in sorted(codes):
        entry = adjusted.get(code)
        if not entry:
            continue
        try:
            raw_dividend = float(entry.get("dividend"))
        except (TypeError, ValueError):
            continue

        active = _active_for_code(staged, code)
        snapshot = staged["feed_snapshots"].get(code)
        if active and snapshot is not None:
            try:
                previous = float(snapshot["dividend"])
            except (KeyError, TypeError, ValueError):
                previous = raw_dividend
            reflected_count = _feed_adjusted_prefix(
                previous, raw_dividend, active, tolerance
            )
            if reflected_count:
                for item in active[:reflected_count]:
                    item["active"] = False
                    item["feed_adjusted_date"] = observed_date
                logger.info(
                    "splits: %s のfeed生値が係数を反映したため %d 件を無効化",
                    code,
                    reflected_count,
                )
                active = _active_for_code(staged, code)

        staged["feed_snapshots"][code] = {
            "dividend": raw_dividend,
            "observed_date": observed_date,
            "feed_year": str(entry.get("year", "")),
        }

        factor = 1.0
        for item in active:
            factor *= float(item["ratio"])
        if factor == 1.0:
            continue

        entry["dividend"] = raw_dividend / factor
        monthly = entry.get("monthly")
        if isinstance(monthly, list):
            entry["monthly"] = [
                (float(value) / factor if value not in (None, "") else value)
                for value in monthly
            ]
        logger.info(
            "splits: %s の配当を累積係数 %s で調整 (%s -> %s)",
            code,
            factor,
            raw_dividend,
            entry["dividend"],
        )
    return adjusted, staged

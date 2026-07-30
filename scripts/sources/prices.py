# 役割: yfinance を使い、銘柄コード群の最新終値(株価)を一括取得する。
#       ティッカーは f"{code}.T"（東証）。チャンク分割で大量呼び出しを回避し、
#       失敗はNaN→空欄(スキップ)として扱い、全体を落とさない。
# 出力:
#   get_prices: dict code(str) -> price(float)
#   get_prices_with_changes: 上記に加え、分割候補となる急変銘柄の直近2終値

import pandas as pd

try:
    import yfinance as yf
except Exception:  # noqa: BLE001
    yf = None


def _extract_close_series(data, ticker):
    """yf.download の結果から、指定ティッカーの有効な終値系列を取り出す。"""
    try:
        # 複数ティッカー時は列が MultiIndex (field, ticker) になる
        if isinstance(data.columns, pd.MultiIndex):
            if ("Close", ticker) not in data.columns:
                return None
            series = data[("Close", ticker)].dropna()
        else:
            # 単一ティッカー時は通常のカラム
            if "Close" not in data.columns:
                return None
            series = data["Close"].dropna()
        if series.empty:
            return None
        return series
    except Exception:  # noqa: BLE001
        return None


def _extract_last_close(data, ticker):
    """yf.download の結果から、指定ティッカーの最新終値を取り出す。"""
    series = _extract_close_series(data, ticker)
    if series is None:
        return None
    return float(series.iloc[-1])


def _is_candidate(ratio, lower, upper):
    return ratio < lower or ratio > upper


def _index_date(value):
    value = value.date() if hasattr(value, "date") else value
    return value.isoformat()


def get_prices_with_changes(
    codes, cfg, logger, previous_prices=None, previous_date=None
):
    """最新終値と、株式分割・併合の候補となる急変を返す。

    株価取得で元々ダウンロードしている直近5営業日の Close と、前回保存した
    database.csv のS列を使い、比が 2/3 未満または 1.5 超の銘柄だけを候補にする。
    Yahooが分割前履歴を遡及修正しても、前回保存値との比較なら検出できる。
    この候補だけ後段で split event を問い合わせるため、全銘柄に対する
    個別API呼び出しを避けられる。
    """
    result = {}
    changes = {}
    if yf is None:
        logger.warning("prices: yfinance が import できませんでした。株価はスキップします")
        return result, changes

    chunk_size = int(cfg.get("yfinance_chunk_size", 250))
    lower = float(cfg.get("split_candidate_min_price_ratio", 2.0 / 3.0))
    upper = float(cfg.get("split_candidate_max_price_ratio", 1.5))
    previous_prices = previous_prices or {}
    codes = [str(c).strip() for c in codes if str(c).strip()]

    for i in range(0, len(codes), chunk_size):
        chunk = codes[i : i + chunk_size]
        tickers = [f"{c}.T" for c in chunk]
        try:
            # 直近数日分を取得し、最新の有効終値を採用（休場日対策で period="5d"）。
            data = yf.download(
                tickers,
                period="5d",
                interval="1d",
                threads=True,
                progress=False,
                group_by="column",
                auto_adjust=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "prices: バッチ %d-%d の取得に失敗(%s)。スキップします",
                i,
                i + len(chunk),
                e,
            )
            continue

        if data is None or len(data) == 0:
            logger.warning("prices: バッチ %d-%d は空データでした", i, i + len(chunk))
            continue

        for code, ticker in zip(chunk, tickers):
            series = _extract_close_series(data, ticker)
            if series is None:
                continue
            latest = float(series.iloc[-1])
            result[code] = latest
            latest_date = _index_date(series.index[-1])

            # Yahooが履歴を未調整で返す場合は、同じレスポンス内の前営業日比で検出。
            if len(series) >= 2:
                previous = float(series.iloc[-2])
                ratio = latest / previous if previous > 0 and latest > 0 else 1.0
            else:
                previous = 0
                ratio = 1.0
            if _is_candidate(ratio, lower, upper):
                changes[code] = {
                    "previous_date": _index_date(series.index[-2]),
                    "latest_date": latest_date,
                    "previous_close": previous,
                    "latest_close": latest,
                    "price_ratio": ratio,
                }

            # Yahooが分割前履歴を遡及修正する場合に備え、前回ビルドで永続化した
            # S列とも比較する。こちらを優先すると実運用で観測した変化を保持できる。
            persisted = previous_prices.get(code)
            try:
                persisted = float(persisted)
            except (TypeError, ValueError):
                persisted = 0
            persisted_ratio = latest / persisted if persisted > 0 and latest > 0 else 1.0
            if _is_candidate(persisted_ratio, lower, upper):
                changes[code] = {
                    "previous_date": previous_date or latest_date,
                    "latest_date": latest_date,
                    "previous_close": persisted,
                    "latest_close": latest,
                    "price_ratio": persisted_ratio,
                }

        logger.info(
            "prices: バッチ %d-%d 完了（累計 %d 件取得）",
            i,
            i + len(chunk),
            len(result),
        )

    logger.info("prices: 株価 %d / %d 銘柄を取得", len(result), len(codes))
    logger.info("prices: 分割・併合の照会候補 %d 件", len(changes))
    return result, changes


def get_prices(codes, cfg, logger):
    """codes(list[str]) の最新終値を dict{code: price} で返す。

    既存の呼び出し側との互換性を保つラッパー。取得できなかった銘柄は
    キーを含めない（呼び出し側で空欄扱い）。
    """
    result, _ = get_prices_with_changes(codes, cfg, logger)
    return result

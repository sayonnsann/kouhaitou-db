# 役割: yfinance を使い、銘柄コード群の最新終値(株価)を一括取得する。
#       ティッカーは f"{code}.T"（東証）。チャンク分割で大量呼び出しを回避し、
#       失敗はNaN→空欄(スキップ)として扱い、全体を落とさない。
# 出力: dict code(str) -> price(float)

import pandas as pd

try:
    import yfinance as yf
except Exception:  # noqa: BLE001
    yf = None


def _extract_last_close(data, ticker):
    """yf.download の結果から、指定ティッカーの最新終値を取り出す。"""
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
        return float(series.iloc[-1])
    except Exception:  # noqa: BLE001
        return None


def get_prices(codes, cfg, logger):
    """codes(list[str]) の最新終値を dict{code: price} で返す。

    取得できなかった銘柄はキーを含めない（呼び出し側で空欄扱い）。
    """
    result = {}
    if yf is None:
        logger.warning("prices: yfinance が import できませんでした。株価はスキップします")
        return result

    chunk_size = int(cfg.get("yfinance_chunk_size", 250))
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
            price = _extract_last_close(data, ticker)
            if price is not None:
                result[code] = price

        logger.info(
            "prices: バッチ %d-%d 完了（累計 %d 件取得）",
            i,
            i + len(chunk),
            len(result),
        )

    logger.info("prices: 株価 %d / %d 銘柄を取得", len(result), len(codes))
    return result

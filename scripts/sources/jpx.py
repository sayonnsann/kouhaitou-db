# 役割: JPX(日本取引所)の東証上場銘柄一覧 data_j.xls をダウンロード・解析し、
#       内国株式(普通株)のみに絞った銘柄ユニバースを返す。
# 出力: list[dict] {code, name, market(市場・商品区分), sector33(33業種区分)}

import io
import re
from urllib.parse import urljoin

import pandas as pd

# 既知の直リンク（ブラウザUAで200 OK確認済み）。まずこれを試す。
JPX_XLS_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)
# 直リンクが失効した場合に辿るインデックスページ。
JPX_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"


def _discover_xls_url(session, logger):
    """インデックスページから data_j.xls への相対リンクを見つけ、絶対URLで返す。"""
    resp = session.get(JPX_INDEX_URL, timeout=30)
    resp.raise_for_status()
    html = resp.text
    # href="....data_j.xls" を正規表現で拾う
    m = re.search(r'href="([^"]*data_j\.xls)"', html)
    if not m:
        raise RuntimeError("インデックスページから data_j.xls のリンクが見つかりません")
    rel = m.group(1)
    abs_url = urljoin(JPX_INDEX_URL, rel)
    logger.info("JPX: インデックスから解決したURL: %s", abs_url)
    return abs_url


def _download_xls(session, logger):
    """data_j.xls のバイト列を返す。直リンク→失敗時はインデックス経由。"""
    # まず既知の直リンク
    try:
        resp = session.get(JPX_XLS_URL, timeout=60)
        resp.raise_for_status()
        logger.info("JPX: 直リンクからダウンロード成功")
        return resp.content
    except Exception as e:  # noqa: BLE001
        logger.warning("JPX: 直リンク失敗(%s)。インデックス経由で再試行します", e)

    # インデックスページからURLを再発見
    url = _discover_xls_url(session, logger)
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    logger.info("JPX: インデックス経由でダウンロード成功")
    return resp.content


def get_universe(session, logger):
    """内国株式のみのユニバースを list[dict] で返す。

    dict のキー: code, name, market, sector33
    失敗時は例外を送出（呼び出し側で捕捉し、部分的成功でCSVを出す想定）。
    """
    content = _download_xls(session, logger)

    # data_j.xls は xlrd で読める旧xls形式。コードは文字列として扱う。
    df = pd.read_excel(io.BytesIO(content), dtype=str)

    # 想定カラム: 日付, コード, 銘柄名, 市場・商品区分, 33業種コード, 33業種区分, ...
    col_code = "コード"
    col_name = "銘柄名"
    col_market = "市場・商品区分"
    col_sector33 = "33業種区分"

    # 必須カラムの存在チェック
    for col in (col_code, col_name, col_market):
        if col not in df.columns:
            raise RuntimeError(
                "JPX: 期待するカラム '%s' が見つかりません。実カラム=%s"
                % (col, list(df.columns))
            )

    # 内国株式のみ残す（ETF/ETN/REIT/優先出資証券/外国株/PRO Marketを除外）。
    # 市場・商品区分に "内国株式" を含む行のみを採用。
    mask = df[col_market].fillna("").str.contains("内国株式")
    df = df[mask].copy()

    universe = []
    for _, row in df.iterrows():
        code = (row.get(col_code) or "").strip()
        if not code:
            continue
        name = (row.get(col_name) or "").strip()
        market = (row.get(col_market) or "").strip()
        sector33 = ""
        if col_sector33 in df.columns:
            sector33 = (row.get(col_sector33) or "").strip()
        universe.append(
            {
                "code": code,
                "name": name,
                "market": market,
                "sector33": sector33,
            }
        )

    logger.info("JPX: 内国株式ユニバース %d 銘柄を取得", len(universe))
    return universe

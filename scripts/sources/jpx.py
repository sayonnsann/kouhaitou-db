# 役割: JPX(日本取引所)の東証上場銘柄一覧 data_j.xls をダウンロード・解析し、
#       内国株式(普通株) + ETF・ETN + REIT等 に絞った銘柄ユニバースを返す。
#       （PRO Market・外国株式・出資証券は含めない）
# 出力: list[dict] {code, name, market(市場・商品区分), sector33(33業種区分), instrument}
#   instrument: "stock"|"etf"|"reit"（市場・商品区分から判定）
#   sector33: 内国株式は33業種名。ETF は "ETF"、REIT は "REIT" の識別ラベルを入れる
#             （33業種区分が "-" のETF/REITを、管理シートの業種列/業種ソートで判別可能にする）。

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


# 市場・商品区分 → instrument 種別の判定。
# ETF・ETN は "etf"、REIT・ベンチャーファンド・カントリーファンド・インフラファンドは "reit"。
def _classify_instrument(market):
    """市場・商品区分の文字列から instrument("stock"|"etf"|"reit") を返す。対象外は None。"""
    if "内国株式" in market:
        return "stock"
    if "ETF" in market or "ETN" in market:
        return "etf"
    # REIT・ベンチャーファンド・カントリーファンド・インフラファンド 一括を reit 扱い
    if "REIT" in market or "ファンド" in market:
        return "reit"
    return None


# instrument → sector33(業種ラベル) の既定値。
# 管理シート(テンプレ)は投信類の業種カテゴリとして「ETF・他」を持ち、業種割合や
# 景気感応度の集計もこの値を前提にしている。テンプレに存在しない "ETF"/"REIT" を入れると
# 業種→景気感応度のvlookupが不整合を起こし、景気感応度グラフが壊れる(#ルートノードエラー)。
# そのため国内ETF・REITはいずれもテンプレ準拠の「ETF・他」に統一する。
_INSTRUMENT_SECTOR_LABEL = {"etf": "ETF・他", "reit": "ETF・他"}


def get_universe(session, logger):
    """内国株式 + ETF・ETN + REIT等 のユニバースを list[dict] で返す。

    dict のキー: code, name, market, sector33, instrument
      - instrument: "stock"|"etf"|"reit"
      - sector33  : 内国株式は33業種名、ETFは "ETF"、REITは "REIT"
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

    # 内国株式 + ETF・ETN + REIT等 を残す（PRO Market/外国株式/出資証券は除外）。
    # instrument 判定が None(=対象外) の行は落とす。
    market_series = df[col_market].fillna("")
    mask = market_series.apply(lambda m: _classify_instrument(m) is not None)
    df = df[mask].copy()

    universe = []
    n_stock = n_etf = n_reit = 0
    for _, row in df.iterrows():
        code = (row.get(col_code) or "").strip()
        if not code:
            continue
        name = (row.get(col_name) or "").strip()
        market = (row.get(col_market) or "").strip()
        instrument = _classify_instrument(market)
        if instrument is None:
            continue

        if instrument == "stock":
            # 内国株式は従来どおり33業種名を使う
            sector33 = ""
            if col_sector33 in df.columns:
                sector33 = (row.get(col_sector33) or "").strip()
            n_stock += 1
        else:
            # ETF/REITは33業種が "-" なので識別ラベルを入れる
            sector33 = _INSTRUMENT_SECTOR_LABEL.get(instrument, "")
            if instrument == "etf":
                n_etf += 1
            else:
                n_reit += 1

        universe.append(
            {
                "code": code,
                "name": name,
                "market": market,
                "sector33": sector33,
                "instrument": instrument,
            }
        )

    logger.info(
        "JPX: ユニバース %d 銘柄を取得（内国株式 %d / ETF %d / REIT %d）",
        len(universe), n_stock, n_etf, n_reit,
    )
    return universe

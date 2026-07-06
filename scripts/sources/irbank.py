# 役割: IR BANK (https://irbank.net/{code}/dividend) の
#       「配当金の状況（円/株）」テーブルをスクレイピングし、
#       現在の予想年間配当・回数・権利確定月を返す。
#
# 返り値: dict {dividend: float, count: int, record_months: list[int]} または None(失敗/スキップ)
#
# --- 検証済みサンプル値（実ページのHTMLを実際にパースして確認済み）---
#   IR BANK の「配当金の状況」表は次の列を持つ:
#     年度 | 区分 | 中間 | 期末 | 合計 | (分割調整) | 配当利回り | 備考
#   ※「予想/実績」の別は 1列目(年度)ではなく 2列目(区分)に入る点に注意。
#   最後の「予想」行が今期予想。年間配当=「合計」列, 回数=中間/期末の有無から判定。
#     8058 三菱商事    : 2027年3月予想 合計125円 (中間62/期末63) 決算月3 回数2
#     9434 ソフトバンク: 2027年3月予想 合計8.80円 (4.40/4.40)   決算月3 回数2
#     8306 三菱UFJ     : 2026年3月予想 合計70円  (中間35/期末35) 決算月3 回数2
#   （※三菱UFJは後日 中間35/期末39=74円 へ増配修正される場合があるが、
#     IR BANK 配当表の「予想」行が更新されるまでは 70円 が取得値。実データ準拠。）
#
# --- robots / マナー ---
#   IR BANK robots.txt は /search のみ Disallow、/{code}/dividend は許可。
#   Crawl-delay 指定なしだが、責任あるスクレイパとして config の
#   request_min_interval_seconds 以上の間隔を空け、タイムアウト+指数バックオフを行う。
#   404/パース失敗は None を返して当該銘柄をスキップする。

import re
import time

from bs4 import BeautifulSoup

# 月ラベル（"3月" 等）を含む予想/実績行を判定する正規表現
RE_YEARMONTH = re.compile(r"(\d{1,2})月")
# 数値（配当額）抽出用。カンマ・円を除去した上で float 化する。
RE_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _to_float(text):
    """テーブルセルの文字列から数値を取り出す。取れなければ None。"""
    if text is None:
        return None
    t = text.replace(",", "").replace("円", "").strip()
    m = RE_NUMBER.search(t)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _get_html(session, code, cfg, logger):
    """{code}/dividend のHTMLを取得。指数バックオフでリトライ。404等はNone。"""
    url = f"https://irbank.net/{code}/dividend"
    timeout = int(cfg.get("request_timeout", 20))
    retry_max = int(cfg.get("retry_max", 3))

    for attempt in range(1, retry_max + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 404:
                logger.info("irbank: %s は404。スキップ", code)
                return None
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001
            wait = 1.5 * (2 ** (attempt - 1))  # 指数バックオフ
            logger.warning(
                "irbank: %s 取得失敗(試行%d/%d, %s)。%.1f秒後に再試行",
                code,
                attempt,
                retry_max,
                e,
                wait,
            )
            if attempt < retry_max:
                time.sleep(wait)
    logger.warning("irbank: %s は %d 回試行しても取得できず。スキップ", code, retry_max)
    return None


def _find_dividend_table(soup):
    """「配当金の状況」テーブルを探す。見出しテキストで近傍のtableを特定する。"""
    # 見出し(見だし要素/キャプション)に "配当金の状況" を含む箇所を探す
    for tag in soup.find_all(string=re.compile("配当金の状況")):
        # その文字列を含む要素の後続 table を探す
        el = tag.parent
        while el is not None:
            table = el.find_next("table")
            if table is not None:
                return table
            el = el.parent
    # 見つからなければ、ページ内の最初のtableをフォールバックで返す
    return soup.find("table")


def _column_index(header_cells, *keywords):
    """ヘッダセル群から、keywords のいずれかを含む列の index を返す。無ければ None。

    ヘッダは "配当 利回り" のように空白入りで来るため、空白を除去して比較する。
    """
    for i, h in enumerate(header_cells):
        norm = h.replace(" ", "").replace("　", "")
        for kw in keywords:
            if kw in norm:
                return i
    return None


def _parse_table(table, code, logger):
    """テーブルから (dividend, count, record_months) を返す。失敗時 None。

    IR BANK「配当金の状況」表の実列: 年度 | 区分 | 中間 | 期末 | 合計 | (分割調整) | 配当利回り | 備考
      - 「予想/実績」の別は 1列目(年度)ではなく 2列目(区分) に入る。
      - 年間配当は「合計」列を **列名で特定** して採る（max() で 分割調整/利回り/性向
        などの無関係な数値を拾わないため）。合計が "-" 等で欠ける場合は 中間+期末 で補う。
      - 回数は 中間・期末 列の値の有無から判定（両方あれば2, 期末のみなら1）。
      - 決算月は 年度列("YYYY年 M月") の月から取得。
      - 最後の「予想」行を今期予想とし、無ければ最新行にフォールバック（ログ出力）。
    """
    if table is None:
        return None

    rows = table.find_all("tr")
    if not rows:
        return None

    # --- ヘッダ行から列位置を特定 ---
    header_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    idx_year = _column_index(header_cells, "年度") or 0
    idx_kubun = _column_index(header_cells, "区分")
    idx_chukan = _column_index(header_cells, "中間")
    idx_kimatsu = _column_index(header_cells, "期末")
    idx_goukei = _column_index(header_cells, "合計")

    # 区分列が取れない場合のみ位置フォールバック（通常は2列目=区分）。
    if idx_kubun is None:
        idx_kubun = 1
    # 【重要】中間/期末は「列自体が存在しない」銘柄がある（年1回=期末のみ配当だと
    #   ヘッダが 年度|区分|期末|合計|… となり中間列が無い）。存在しない列を位置決め打ちで
    #   拾うと期末を中間と誤読して回数を誤判定するため、None のまま＝列なしとして扱う。
    # 合計列は通常存在するが、無いテーブルでは期末列を年間配当の代替とする。
    if idx_goukei is None:
        idx_goukei = idx_kimatsu

    def cell(cells, i):
        return cells[i].get_text(" ", strip=True) if (i is not None and i < len(cells)) else ""

    # --- データ行を走査 ---
    parsed = []  # (year_label, is_forecast, month, chukan, kimatsu, goukei)
    for tr in rows[1:]:
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        year_text = cell(cells, idx_year)
        mmonth = RE_YEARMONTH.search(year_text)
        if not mmonth:
            continue  # 月が取れない行（注記等）は無視
        month = int(mmonth.group(1))
        kubun = cell(cells, idx_kubun)
        is_forecast = "予想" in kubun
        chukan = _to_float(cell(cells, idx_chukan))
        kimatsu = _to_float(cell(cells, idx_kimatsu))
        goukei = _to_float(cell(cells, idx_goukei))
        parsed.append((year_text, is_forecast, month, chukan, kimatsu, goukei))

    if not parsed:
        logger.info("irbank: %s テーブルから対象行を抽出できず。スキップ", code)
        return None

    # 1行分の年間配当(合計。欠ける場合は中間+期末で補完)を返すヘルパ。有効値が無ければ None。
    def _row_dividend(r):
        _, _, _, ch, ki, go = r
        d = go
        if d is None or d <= 0:
            parts = [v for v in (ch, ki) if v is not None and v > 0]
            d = sum(parts) if parts else None
        return d if (d is not None and d > 0) else None

    # 行を「新しい順」に見て、有効な配当額を持つ最初の行を採用する。
    # parsed は古い→新しい順（末尾＝最新の予想行）なので、reversed で最新から探す。
    # → 今期予想に値があればそれ、予想が「未定/空」なら直近の“実績”配当を採用する
    #   （ユーザー要望: 予想が無いものは実績値を入れる）。全行無配なら None。
    chosen = None
    for r in reversed(parsed):
        d = _row_dividend(r)
        if d is not None:
            chosen = (r, d)
            break
    if chosen is None:
        logger.info("irbank: %s 有効な配当額なし(無配等)。スキップ", code)
        return None
    (year_text, is_forecast, month, chukan, kimatsu, goukei), dividend = chosen
    kubun_label = "予想" if is_forecast else "実績"

    # --- 回数の判定（中間・期末の有無で判定） ---
    has_chukan = chukan is not None and chukan > 0
    has_kimatsu = kimatsu is not None and kimatsu > 0
    if has_chukan and has_kimatsu:
        count = 2
    elif has_kimatsu or has_chukan:
        count = 1
    else:
        # どちらも取れないが合計だけある → 期末一括(年1回)とみなす
        count = 1

    # --- 権利確定月の推定: month(決算月=年度末) と count から導く ---
    record_months = _infer_record_months(month, count)

    logger.info(
        "irbank: %s %s=%s 合計=%s 中間=%s 期末=%s 決算月=%d 回数=%d 権利月=%s",
        code, kubun_label, year_text, dividend, chukan, kimatsu, month, count, record_months,
    )
    return {"dividend": dividend, "count": count, "record_months": record_months}


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


def fetch_one(session, code, cfg, logger):
    """1銘柄分の配当情報を取得。失敗/スキップ時は None。"""
    html = _get_html(session, code, cfg, logger)
    if html is None:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        table = _find_dividend_table(soup)
        return _parse_table(table, code, logger)
    except Exception as e:  # noqa: BLE001
        logger.warning("irbank: %s のパースで例外(%s)。スキップ", code, e)
        return None


def fetch_dividends(session, codes, cfg, logger):
    """codes(list[str]) を順にスクレイピングし、dict{code: {dividend,count,record_months}} を返す。

    取得できた銘柄のみ格納。リクエスト間は request_min_interval_seconds 以上空ける。
    """
    interval = float(cfg.get("request_min_interval_seconds", 1.8))
    result = {}
    total = len(codes)
    for idx, code in enumerate(codes, 1):
        try:
            info = fetch_one(session, code, cfg, logger)
            if info is not None:
                result[str(code)] = info
        except Exception as e:  # noqa: BLE001
            # 個別銘柄の例外で全体を落とさない
            logger.warning("irbank: %s で予期せぬ例外(%s)。スキップ", code, e)
        # 最後の1件の後は待たない
        if idx < total:
            time.sleep(interval)
    logger.info("irbank: %d / %d 銘柄の配当情報を取得", len(result), total)
    return result

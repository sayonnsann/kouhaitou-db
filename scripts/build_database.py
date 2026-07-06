# 役割: 本プロジェクトのオーケストレーション（統括処理）。
#   1. config ロード
#   2. JPX から ユニバース取得（内国株式 + ETF・ETN + REIT等）
#   3. yfinance で全銘柄の株価取得
#   4. 差分ローテーション + 優先銘柄 で配当取得対象を選定
#   5. instrument 種別で配当取得を振り分け:
#        - stock : IR BANK（従来どおり、挙動を変えない）
#        - etf/reit : yfinance_div（分配金を yfinance から取得）
#      → キャッシュ更新
#   6. 全ユニバースについて 19列(A〜S)の行を構築
#      （配当=キャッシュ、株価=prices、月次=stockはutil.build_monthly / ETF・REITは実支払月）
#   7. data/database.csv を出力（UTF-8 BOMなし, ヘッダS1=最終更新日時）
#   8. data/dividends_cache.csv を出力（新列 monthly / source を後方互換で追加）
#
# 堅牢性: 各ステージを try/except で包み、一部失敗でも取得済みデータでCSVを出す。

import argparse
import csv
import os
import sys

# scripts/ を import パスに追加（sources サブパッケージ解決のため）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import util
from sources import irbank, jpx, prices, yfinance_div

# リポジトリルート（このファイル=scripts/build_database.py の1つ上）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# database.csv のヘッダ（A1〜R1）。S1 は最終更新日時（別途動的に入れる）。
HEADER_A_TO_R = [
    "銘柄コード",   # A
    "銘柄名",       # B
    "市場区分",     # C（未使用枠。市場区分を入れておく）
    "業種",         # D（東証33業種区分）
    "年間配当金",   # E
    "年間配当回数", # F
    "1月",          # G
    "2月",          # H
    "3月",          # I
    "4月",          # J
    "5月",          # K
    "6月",          # L
    "7月",          # M
    "8月",          # N
    "9月",          # O
    "10月",         # P
    "11月",         # Q
    "12月",         # R
]

# キャッシュのカラム。後方互換のため末尾に monthly / source を追加。
#   monthly: G〜R(12値)をセミコロン区切り。ETF/REIT の実支払月・実額。空なら未設定。
#   source : "irbank"(内国株式) | "yfinance"(ETF/REIT)
CACHE_HEADER = ["code", "dividend", "count", "record_months", "fetched_date", "monthly", "source"]


def _abspath(rel):
    return os.path.join(REPO_ROOT, rel)


def load_priority_codes(cfg, logger):
    """priority_codes.txt を読み、コード文字列のリストを返す。"""
    path = _abspath(cfg.get("priority_codes_path", "config/priority_codes.txt"))
    codes = []
    if not os.path.exists(path):
        logger.info("priority: %s が無いので優先銘柄なし", path)
        return codes
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 行内コメント（"8058  # 三菱商事"）を除去
            token = line.split("#", 1)[0].strip()
            if token:
                codes.append(token)
    logger.info("priority: 優先銘柄 %d 件", len(codes))
    return codes


def _parse_monthly(raw):
    """セミコロン区切りの monthly 文字列を長さ12のfloatリストにする。無効なら None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    parts = raw.split(";")
    if len(parts) != 12:
        return None
    months = []
    for p in parts:
        p = p.strip()
        try:
            months.append(float(p) if p else 0.0)
        except ValueError:
            months.append(0.0)
    return months


def load_cache(cfg, logger):
    """dividends_cache.csv を読み、dict{code: {dividend,count,record_months,fetched_date,monthly,source}} を返す。

    新列 monthly / source は旧CSVには無いため get で欠損許容（後方互換）。
    monthly は 12値あればリスト、無ければ None。
    """
    path = _abspath(cfg.get("output_cache_path", "data/dividends_cache.csv"))
    cache = {}
    if not os.path.exists(path):
        logger.info("cache: %s が無いので空キャッシュから開始", path)
        return cache
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = (row.get("code") or "").strip()
                if not code:
                    continue
                rec_raw = (row.get("record_months") or "").strip()
                record_months = []
                if rec_raw:
                    for p in rec_raw.split(";"):
                        p = p.strip()
                        if p.isdigit():
                            record_months.append(int(p))
                cache[code] = {
                    "dividend": row.get("dividend") or "",
                    "count": row.get("count") or "",
                    "record_months": record_months,
                    "fetched_date": (row.get("fetched_date") or "").strip(),
                    # 新列（旧CSVには無いので get で欠損許容）
                    "monthly": _parse_monthly(row.get("monthly")),
                    "source": (row.get("source") or "").strip(),
                }
    except Exception as e:  # noqa: BLE001
        logger.warning("cache: 読み込み失敗(%s)。空キャッシュから開始", e)
        return {}
    logger.info("cache: %d 件のキャッシュを読み込み", len(cache))
    return cache


def select_rotation_targets(universe, cache, batch_size, priority_codes, logger):
    """IR BANK 取得対象コードのリストを返す。

    (a) fetched_date が古い順の batch_size 件 + (b) 優先銘柄 のユニオン。
    キャッシュに無い（未取得）銘柄は fetched_date="" とみなし最優先で先頭に来る。
    """
    universe_codes = [s["code"] for s in universe]
    universe_set = set(universe_codes)

    def fetched_key(code):
        entry = cache.get(code)
        # 未取得(キャッシュなし or 日付空)は "" となり最古扱い→先頭に来る
        return entry.get("fetched_date", "") if entry else ""

    # 古い順（fetched_date昇順）にソート。空文字が最小なので未取得が先頭。
    ordered = sorted(universe_codes, key=fetched_key)
    rotation = ordered[: max(0, int(batch_size))]

    # 優先銘柄（ユニバースに存在するもののみ）を必ず含める
    priority_valid = [c for c in priority_codes if c in universe_set]

    # ユニオン（順序維持: rotation → priority の順で重複排除）
    targets = []
    seen = set()
    for c in list(rotation) + priority_valid:
        if c not in seen:
            seen.add(c)
            targets.append(c)

    logger.info(
        "rotation: 取得対象 %d 件（ローテ %d + 優先 %d, 重複排除後）",
        len(targets),
        len(rotation),
        len(priority_valid),
    )
    return targets


def update_cache(cache, fresh, logger):
    """新規取得結果 fresh を cache にマージし、fetched_date を更新する。

    fresh の各 info は次のいずれかの形:
      - IR BANK  : {dividend, count, record_months}                （source="irbank"）
      - yfinance : {dividend, count, months12, source="yfinance"}  （ETF/REIT, 実支払月）
    月別(monthly)は yfinance のときだけ months12(長さ12) を保持する。
    """
    today = util.datetime.now(util.JST).strftime("%Y-%m-%d")
    for code, info in fresh.items():
        months12 = info.get("months12")
        monthly = list(months12) if (months12 and len(months12) == 12) else None
        cache[code] = {
            "dividend": info.get("dividend", ""),
            "count": info.get("count", ""),
            "record_months": info.get("record_months", []) or [],
            "fetched_date": today,
            "monthly": monthly,
            "source": info.get("source", "irbank"),
        }
    logger.info("cache: %d 件を更新（fetched_date=%s）", len(fresh), today)
    return cache


def write_cache(cache, cfg, logger):
    """dividends_cache.csv を UTF-8(BOMなし) で書き出す。"""
    path = _abspath(cfg.get("output_cache_path", "data/dividends_cache.csv"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CACHE_HEADER)
        for code in sorted(cache.keys()):
            entry = cache[code]
            rec = ";".join(str(m) for m in entry.get("record_months", []) or [])
            monthly = entry.get("monthly")
            monthly_str = ""
            if monthly and len(monthly) == 12:
                monthly_str = ";".join(_fmt_num(m) if m else "0" for m in monthly)
            writer.writerow(
                [
                    code,
                    entry.get("dividend", ""),
                    entry.get("count", ""),
                    rec,
                    entry.get("fetched_date", ""),
                    monthly_str,
                    entry.get("source", ""),
                ]
            )
    logger.info("cache: %s に %d 件を書き出し", path, len(cache))


def _fmt_num(x):
    """CSV出力用の数値整形。0はそのまま、整数値は小数点を落とす。"""
    if x is None or x == "":
        return ""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    if f == int(f):
        return str(int(f))
    # 小数は不要な0を落として整形
    return ("%f" % f).rstrip("0").rstrip(".")


def build_rows(universe, price_map, cache, cfg, logger):
    """全ユニバースについて 19列(A〜S) の行リストを構築して返す。"""
    month_basis = cfg.get("month_basis", "payment")
    value_mode = cfg.get("value_mode", "amount")

    rows = []
    for stock in universe:
        code = stock["code"]
        name = stock.get("name", "")
        market = stock.get("market", "")
        sector33 = stock.get("sector33", "")

        entry = cache.get(code)
        annual = ""
        count = ""
        record_months = []
        cached_monthly = None
        if entry:
            annual = entry.get("dividend", "")
            count = entry.get("count", "")
            record_months = entry.get("record_months", []) or []
            cached_monthly = entry.get("monthly")

        # 月次配当(G〜R)を構築。
        #   ETF/REIT: キャッシュに monthly(12値=実支払月・実額) があればそれをそのまま使う。
        #   stock   : 従来どおり util.build_monthly で年間配当を割り付ける。
        if cached_monthly and len(cached_monthly) == 12:
            monthly = cached_monthly
        else:
            monthly = util.build_monthly(
                annual if annual != "" else 0,
                count if count != "" else 0,
                record_months,
                month_basis=month_basis,
                value_mode=value_mode,
            )

        # 株価(S列)
        price = price_map.get(code, "")

        row = [
            code,                     # A 銘柄コード
            name,                     # B 銘柄名
            market,                   # C 未使用枠→市場区分
            sector33,                 # D 業種(33業種区分)
            _fmt_num(annual),         # E 年間配当金
            _fmt_num(count),          # F 年間配当回数
        ]
        # G〜R 1月〜12月
        for m in monthly:
            # value_mode=flag のときは 1/0、amount のときは金額。0は空欄にせず0で出す
            row.append(_fmt_num(m) if m else "0")
        row.append(_fmt_num(price))   # S 株価
        rows.append(row)

    logger.info("build: %d 行を構築", len(rows))
    return rows


def write_database(rows, cfg, logger):
    """database.csv を UTF-8(BOMなし) で書き出す。ヘッダ S1 = 最終更新日時。"""
    path = _abspath(cfg.get("output_database_path", "data/database.csv"))
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # ヘッダ行: A1〜R1 は通常ラベル、S1 は最終更新日時タイムスタンプ
    header = list(HEADER_A_TO_R) + [util.jst_timestamp()]

    # encoding="utf-8" は BOM を付けない（"utf-8-sig" だとBOMが付くので使わない）
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    logger.info("database: %s に %d 銘柄を書き出し（S1=%s）", path, len(rows), header[18])


def parse_args(argv=None):
    """CLI引数を解釈する。
    --limit N : 先頭N銘柄だけ処理（ローカル動作確認用）
    --config PATH : 使用する config.yaml のパス
    """
    p = argparse.ArgumentParser(description="高配当株DB ビルド")
    p.add_argument("--limit", type=int, default=None,
                   help="先頭N銘柄だけ処理（ローカル動作確認用）")
    p.add_argument("--config", type=str, default=None,
                   help="config.yaml のパス（未指定なら config/config.yaml）")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logger = util.get_logger()
    logger.info("=== 高配当株DB ビルド開始 ===")

    # --- config ---
    try:
        cfg = util.load_config(args.config)
    except Exception as e:  # noqa: BLE001
        logger.error("config の読み込みに失敗(%s)。既定値で続行", e)
        cfg = {}

    session = util.build_session(cfg)

    # --- JPX ユニバース ---
    universe = []
    try:
        universe = jpx.get_universe(session, logger)
    except Exception as e:  # noqa: BLE001
        logger.error("JPX ユニバース取得に失敗(%s)", e)
        # ユニバースが無いと何も出せないが、既存キャッシュから最低限を出す試み
        universe = []

    # キャッシュ読み込み（ユニバース取得失敗時のフォールバックにも使う）
    cache = load_cache(cfg, logger)

    # ユニバースが空なら、キャッシュに存在するコードだけで最低限のユニバースを作る
    if not universe and cache:
        logger.warning("JPX失敗のため、キャッシュ内コードで暫定ユニバースを構築")
        universe = [
            {"code": c, "name": "", "market": "", "sector33": "",
             "instrument": cache[c].get("source") == "yfinance" and "etf" or "stock"}
            for c in cache.keys()
        ]

    # --limit: 先頭N銘柄に絞る（ローカル動作確認用）
    if args.limit is not None and args.limit > 0 and universe:
        universe = universe[: args.limit]
        logger.info("limit: 先頭 %d 銘柄に制限", len(universe))

    universe_codes = [s["code"] for s in universe]

    # --- 株価（全銘柄, 毎回フル取得） ---
    price_map = {}
    try:
        price_map = prices.get_prices(universe_codes, cfg, logger)
    except Exception as e:  # noqa: BLE001
        logger.error("株価取得に失敗(%s)。株価は空欄で続行", e)
        price_map = {}

    # --- IR BANK 取得対象の選定（差分ローテ + 優先銘柄） ---
    fresh = {}
    try:
        batch_size = int(os.environ.get("DIVIDEND_BATCH_SIZE", cfg.get("dividend_batch_size", 200)))
    except (TypeError, ValueError):
        batch_size = int(cfg.get("dividend_batch_size", 200))

    # instrument 種別を引くためのマップ（未指定は "stock" 扱いで従来挙動を維持）
    instrument_map = {s["code"]: s.get("instrument", "stock") for s in universe}

    try:
        priority_codes = load_priority_codes(cfg, logger)
        targets = select_rotation_targets(universe, cache, batch_size, priority_codes, logger)

        # instrument でIR BANK対象(stock)とyfinance対象(etf/reit)に振り分け
        stock_targets = [c for c in targets if instrument_map.get(c, "stock") == "stock"]
        yf_targets = [c for c in targets if instrument_map.get(c, "stock") in ("etf", "reit")]

        # --- 内国株式: IR BANK（従来どおり、挙動を変えない） ---
        try:
            stock_fresh = irbank.fetch_dividends(session, stock_targets, cfg, logger)
            for code, info in stock_fresh.items():
                info.setdefault("source", "irbank")
                fresh[code] = info
        except Exception as e:  # noqa: BLE001
            logger.error("IR BANK 取得ステージで失敗(%s)。キャッシュのみで続行", e)

        # --- ETF/REIT: yfinance_div（分配金を yfinance から取得） ---
        try:
            yf_fresh = yfinance_div.fetch_dividends(yf_targets, cfg, logger)
            for code, info in yf_fresh.items():
                info.setdefault("source", "yfinance")
                fresh[code] = info
        except Exception as e:  # noqa: BLE001
            logger.error("yfinance 分配金取得ステージで失敗(%s)。キャッシュのみで続行", e)
    except Exception as e:  # noqa: BLE001
        logger.error("配当取得対象の選定で失敗(%s)。キャッシュのみで続行", e)
        fresh = {}

    # --- キャッシュ更新 ---
    try:
        cache = update_cache(cache, fresh, logger)
    except Exception as e:  # noqa: BLE001
        logger.error("キャッシュ更新に失敗(%s)", e)

    # --- 行構築 & 出力 ---
    try:
        rows = build_rows(universe, price_map, cache, cfg, logger)
    except Exception as e:  # noqa: BLE001
        logger.error("行構築に失敗(%s)。空行で出力を試みます", e)
        rows = []

    try:
        write_database(rows, cfg, logger)
    except Exception as e:  # noqa: BLE001
        logger.error("database.csv の書き出しに失敗(%s)", e)

    try:
        write_cache(cache, cfg, logger)
    except Exception as e:  # noqa: BLE001
        logger.error("dividends_cache.csv の書き出しに失敗(%s)", e)

    logger.info("=== 高配当株DB ビルド終了 ===")


if __name__ == "__main__":
    main()

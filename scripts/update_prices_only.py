# 役割: 株価(S列)だけを軽量に更新するオーケストレーション。
#
#   毎朝6:00の scripts/build_database.py（JPXユニバース取得・EDINET配当再構築・
#   分割検知を含むフルビルド）とは別に、1日2回、東証の市場セッションに合わせて
#   株価だけを差し替えたいときに使う。
#
#   --session morning_open   : 前場寄付（当日9:00の始値）を採用
#   --session afternoon_close: 後場引け（大引け＝当日の終値）を採用
#
#   セッション名（morning_open/afternoon_close）にしているのは、東証の取引時間が
#   将来変わっても「前場の寄り値」「後場の引け値」という意味が保たれるようにするため。
#
#   処理:
#     1. 既存の data/database.csv を読み込む（無ければ何もできないので終了）
#     2. A列(銘柄コード)ごとに yfinance で株価を取得する
#          morning_open   : 当日(JST)の始値のみ採用。当日分がまだ無い銘柄
#                           （薄商い・未寄り付き・取得失敗）は前回値を保持する
#          afternoon_close: 直近の有効な終値を採用（従来のフルビルドと同じ）
#     3. 取得できた銘柄だけS列(19列目)を差し替える。B〜R列（銘柄名・業種・
#        配当データ等）は一切変更しない
#     4. ヘッダS1（最終更新日時）を実行時刻(JST)に更新する
#     5. data/price_update_meta.json に {session, session_label, as_of_date,
#        updated_at, ...} を書く（19列のCSV契約は変更しない。配信先が
#        「2026年8月13日 前場寄付時点」のようなラベルを組み立てるための補助ファイル）
#
#   失敗時の挙動:
#     - 1銘柄も取得できなかった場合、CSVは一切書き換えずログにwarningを出す。
#       exit codeは0のまま（yfinance側の一時的な不調でActionsを毎回赤くしない）。
#     - 一部銘柄の取得失敗は、その銘柄だけ前回値を保持して静かに続行する。
#     - このスクリプトは data/split_adjustments.json や
#       data/etf_dividends_cache.csv には一切触れない。

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import util  # noqa: E402
from build_database import _fmt_num  # noqa: E402  (数値整形ロジックを共有)
from sources import prices  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# database.csv は19列（A〜S）。S列(0-indexで18)が株価。
PRICE_COLUMN_INDEX = 18
EXPECTED_COLUMN_COUNT = 19

# セッション定義: 値は yfinance のフィールド名・当日限定判定・表示ラベル。
# 表示ラベルは配信先(pdd等)が「YYYY年M月D日 <ラベル>時点」のような文言を
# 組み立てられるよう、market-session ベースの日本語名にしている。
SESSION_DEFS = {
    "morning_open": {
        "yf_field": "Open",
        "require_today": True,
        "label": "前場寄付",
    },
    "afternoon_close": {
        "yf_field": "Close",
        "require_today": False,
        "label": "後場引け",
    },
}


def _abspath(rel):
    return os.path.join(REPO_ROOT, rel)


def _gha_warning(message):
    """GitHub Actions上ならワークフローに warning アノテーションを出す（失敗にはしない）。"""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # 改行はAnnotationを壊すので単一行に丸める
        print(f"::warning::{message.replace(chr(10), ' ')}")


def load_database(cfg, logger):
    """既存 database.csv を (header, body_rows) で返す。無ければ (None, None)。"""
    path = _abspath(cfg.get("output_database_path", "data/database.csv"))
    if not os.path.exists(path):
        logger.error(
            "update_prices_only: %s が存在しません。先にフルビルド(build_database.py)を"
            "1回実行してください", path,
        )
        return None, None
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            all_rows = list(reader)
    except (OSError, csv.Error) as e:
        logger.error("update_prices_only: %s の読み込みに失敗(%s)", path, e)
        return None, None
    if not all_rows:
        logger.error("update_prices_only: %s が空です", path)
        return None, None
    header, body = all_rows[0], all_rows[1:]
    return header, body


def write_database(header, body, cfg, logger):
    path = _abspath(cfg.get("output_database_path", "data/database.csv"))
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(body)
    logger.info("update_prices_only: %s を書き出し（S1=%s）", path, header[PRICE_COLUMN_INDEX])


def write_meta(session, session_label, updated, total, cfg, logger):
    """pdd側などが「前場寄付/後場引け」を区別して表示したい場合に読める補助ファイル。

    database.csv の19列フォーマットは変更しないため、別ファイルに逃がす。
    as_of_date は updated_at の日付部分（JST）。
    「2026年8月13日 前場寄付時点」のような文言を配信先で組み立てやすいように
    session_label と as_of_date を分けて持たせている。
    """
    path = _abspath(cfg.get("price_update_meta_path", "data/price_update_meta.json"))
    updated_at = util.jst_timestamp()
    payload = {
        "session": session,              # "morning_open" または "afternoon_close"
        "session_label": session_label,  # "前場寄付" または "後場引け"
        "as_of_date": updated_at.split(" ", 1)[0].replace("/", "-"),  # "2026-08-13"
        "updated_at": updated_at,
        "updated_count": updated,
        "total_count": total,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        logger.info("update_prices_only: %s を更新", path)
    except OSError as e:
        # メタファイルは補助情報なので、失敗してもCSV更新自体は成功として扱う
        logger.warning("update_prices_only: メタファイル書き出しに失敗(%s)", e)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="株価(S列)だけを更新する軽量ビルド")
    p.add_argument(
        "--session",
        choices=list(SESSION_DEFS.keys()),
        required=True,
        help="morning_open=前場寄付(当日始値)を採用 / afternoon_close=後場引け(終値)を採用",
    )
    p.add_argument("--config", type=str, default=None,
                   help="config.yaml のパス（未指定なら config/config.yaml）")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    session_def = SESSION_DEFS[args.session]
    session_label = session_def["label"]
    logger = util.get_logger()
    logger.info(
        "=== 株価のみ更新 開始（session=%s / %s） ===", args.session, session_label
    )

    try:
        cfg = util.load_config(args.config)
    except Exception as e:  # noqa: BLE001
        logger.error("config の読み込みに失敗(%s)。既定値で続行", e)
        cfg = {}

    header, body = load_database(cfg, logger)
    if header is None:
        # ベースとなるCSVが無い＝このモードでは何もできない実質的な失敗
        sys.exit(1)

    codes = []
    for row in body:
        if row and row[0].strip():
            codes.append(row[0].strip())

    try:
        price_map = prices.get_field_prices(
            codes,
            cfg,
            logger,
            field=session_def["yf_field"],
            require_today=session_def["require_today"],
        )
    except Exception as e:  # noqa: BLE001
        logger.error("update_prices_only: 株価取得で例外(%s)", e)
        price_map = {}

    if not price_map:
        msg = (
            f"update_prices_only: 株価を1件も取得できませんでした"
            f"(session={args.session}/{session_label})。CSVは更新せず終了します"
        )
        logger.warning(msg)
        _gha_warning(msg)
        return  # exit code 0（ジョブは失敗させない）

    updated = 0
    new_body = []
    for row in body:
        new_row = list(row)
        if not new_row or not new_row[0].strip():
            new_body.append(new_row)
            continue
        code = new_row[0].strip()
        # 想定外に列数が足りない行があっても19列に揃えてから触る（他用途CSVとの整合維持）
        while len(new_row) < EXPECTED_COLUMN_COUNT:
            new_row.append("")
        if code in price_map:
            new_row[PRICE_COLUMN_INDEX] = _fmt_num(price_map[code])
            updated += 1
        new_body.append(new_row)

    new_header = list(header)
    while len(new_header) < EXPECTED_COLUMN_COUNT:
        new_header.append("")
    new_header[PRICE_COLUMN_INDEX] = util.jst_timestamp()

    write_database(new_header, new_body, cfg, logger)
    write_meta(args.session, session_label, updated, len(codes), cfg, logger)

    logger.info(
        "update_prices_only: %d/%d 銘柄の株価を更新（session=%s/%s, 残りは前回値を保持）",
        updated, len(codes), args.session, session_label,
    )
    if updated < len(codes):
        skipped = len(codes) - updated
        logger.info(
            "update_prices_only: %d件は取得できず前回値のまま（session=%s/%s）",
            skipped, args.session, session_label,
        )

    logger.info("=== 株価のみ更新 終了 ===")


if __name__ == "__main__":
    main()

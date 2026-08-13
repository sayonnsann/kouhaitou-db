# 高配当株 管理シート用 自作外部データベース（kouhaitou-db）

日本株の「高配当株 管理シート」（Googleスプレッドシート）が `IMPORTRANGE` で参照している外部「データベース」シートを、**自分で所有・自動更新できる形に差し替える**ためのパイプライン一式です。

既存DBが「更新停止・配当反映が遅い」問題を解消するために、以下を自前で行います。

- 銘柄ユニバース・銘柄名・業種：**JPX公式「東証上場銘柄一覧」（data_j.xls）**
- 株価：**yfinance**（`{code}.T`）
- 配当（年間配当・回数・権利確定月）：**自前のEDINET配信データ**（金融庁EDINETの有価証券報告書由来／分割反映の遅延は本パイプラインで補正／再配布自由）

生成した `database.csv` を GitHub 経由で **jsDelivr** に載せ、Googleスプレッドシート側は `IMPORTDATA` で読み込むだけ。管理シートは **`集計!P2` セルのIDを差し替えるだけ**で動きます。

---

## 仕組みの全体像

```
┌─────────────────────────────────────────────────────────────────────┐
│  GitHub Actions（毎日 早朝JST に1回 cron 実行）                        │
│                                                                     │
│   scripts/build_database.py（オーケストレーション）                    │
│      │                                                              │
│      ├─ sources/jpx.py     → data_j.xls DL・内国株式のみ抽出          │
│      │                        （コード / 銘柄名 / 33業種区分）          │
│      ├─ sources/prices.py  → yfinance バッチで直近終値               │
│      ├─ sources/edinet_feed.py → 自前EDINET配信データ（配当）          │
│      │                        （★2つ目のcheckoutをローカル読み＝毎日全銘柄）│
│      └─ sources/yfinance_div.py → ETF・REITの分配金（差分ローテーション）│
│      │                                                              │
│      ▼                                                              │
│   data/database.csv            （A〜S列・19列・1行目ヘッダ, S1=最終更新日時）│
│   data/etf_dividends_cache.csv （ETF・REIT分配金キャッシュ）             │
│      │  ← git add & commit（data/ を永続化）                         │
└──────┼──────────────────────────────────────────────────────────────┘
       │
       ▼
   GitHub リポジトリ（main ブランチ）
       │
       ▼
   jsDelivr CDN
   https://cdn.jsdelivr.net/gh/sayonnsann/kouhaitou-db@main/data/database.csv
       │
       ▼
   ┌───────────────────────────────────────────────┐
   │ 新規スプレッドシート「データベース」シート        │
   │  A1: =IMPORTDATA("...database.csv")            │
   │      → A1:S… にCSV全体を展開                    │
   └───────────────────────────────────────────────┘
       │  この新スプレッドシートのID
       ▼
   ┌───────────────────────────────────────────────┐
   │ 既存の「管理シート」                             │
   │  集計!P2 セルのIDを ↑ の新DBのIDに書き換えるだけ  │
   │  （IMPORTRANGE の参照先が切り替わる）            │
   └───────────────────────────────────────────────┘
```

---

## 「データベース」シートの列契約（絶対厳守）

`database.csv` は次の19列（A〜S）で出力されます。**1行目はヘッダ、2行目以降が銘柄データ**。管理シートのVLOOKUPは完全一致（FALSE）なので、見出し行があっても問題ありません。

| 列 | 内容 |
|----|------|
| A (1) | 銘柄コード（4桁・VLOOKUPキー） |
| B (2) | 銘柄名 |
| C (3) | 未使用（列位置維持のため必ず存在。市場区分が入る場合あり） |
| D (4) | 業種（東証33業種区分の名称。例：卸売業／食料品／銀行業） |
| E (5) | 年間配当金（1株あたり・今期予想・円） |
| F (6) | 年間配当回数（例：2） |
| G〜R (7〜18) | 1月〜12月の配当（月別・支払月に金額を配置） |
| S (19) | 株価（直近終値・円） |

**★重要：ヘッダ行の S1 セルには「最終更新日時」が入ります。**
管理シートが `IMPORTRANGE(ID,"データベース!S1")` を更新時刻として読むためです。2行目以降のS列は株価です。

---

## ローカル実行手順

```bash
cd /path/to/kouhaitou-db

# 仮想環境
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 依存インストール
pip install -r requirements.txt

# 実行（data/database.csv と data/etf_dividends_cache.csv が生成される）
python scripts/build_database.py
```

内国株式の配当は配信データをローカル読みするだけなので、**毎回すべての銘柄が更新**されます（外部サイトへのアクセスは発生しません）。ローカル実行では配信データの場所を渡してください。

```bash
# 配信リポジトリを手元にクローンしてある場合
python scripts/build_database.py --feed-dir /path/to/pharmacistlife-dividend-data/edinet
# 環境変数でも指定できる
EDINET_FEED_DIR=/path/to/.../edinet python scripts/build_database.py
```

`--feed-dir` も `EDINET_FEED_DIR` も無い場合は jsDelivr から1銘柄ずつ取りにいきます（動作確認用。3,800回のHTTPアクセスになるので通常運用では使いません）。

ETF・REITの分配金だけは yfinance を叩くため、従来どおり `dividend_batch_size`（既定200）＋優先銘柄ぶんの差分ローテーションで更新します。

### 主な設定（`config/config.yaml`）

| キー | 意味 | 既定 |
|------|------|------|
| `user_agent` | JPX / jsDelivr へ送る User-Agent。**連絡先メールを必ず自分のものに書き換える** | プレースホルダ |
| `edinet_feed_dir` | 配信データ（`{コード}.json`）のディレクトリ | 空（環境変数 `EDINET_FEED_DIR` / `--feed-dir` で指定） |
| `request_timeout` / `retry_max` | タイムアウト／リトライ回数 | — |
| `dividend_batch_size` | 1回に yfinance へ分配金を問い合わせる「最も古いN銘柄」（ETF・REITのみ） | 200（環境変数 `DIVIDEND_BATCH_SIZE` で上書き可） |
| `month_basis` | `payment`（支払月＝権利確定+約3ヶ月・既定）／`record`（権利確定月ベース） | payment |
| `value_mode` | `amount`（金額・既定）／`flag`（1/0） | amount |
| `split_candidate_min_price_ratio` / `split_candidate_max_price_ratio` | split eventを個別照会する前営業日比の範囲外閾値 | 2/3 / 1.5 |
| `split_price_validation_tolerance` | event比率と実株価変動の整合許容差 | 0.20 |
| `split_feed_transition_tolerance` | feed側が係数を反映したとみなす許容差 | 0.15 |

**優先銘柄**は `config/priority_codes.txt` に1行1コードで記載（保有・監視銘柄）。内国株式は毎回すべて更新されるため、この指定が効くのは ETF・REIT だけです。

### 株式分割・併合の自動調整

株価の一括取得で前営業日比が急変した内国株式だけ、yfinance の
`Stock Splits` event を追加照会します。event 比率と実際の株価変動が整合した
場合に限り、EDINET由来の年間配当と月別配当を同じ比率で調整します。
照合不能・不整合はデータを出力せずビルドを失敗させます。
急変判定には直近履歴に加え、Yahooによる過去値の遡及修正に備えて前回
`database.csv` に保存したS列も使います。

検証済み係数は `data/split_adjustments.json` に永続化されます。EDINET feed は
毎日元値から再取得されるため active な係数を毎回適用し、feed の生値自体が
係数ぶん変化した時点で feed 側調整済みと判定して係数を無効化します。

---

## GitHub Actions（自動運用）

`.github/workflows/update.yml` が毎日1回（既定：UTC 21:00 = **JST 6:00**）cronで実行され、`data/database.csv`、`data/etf_dividends_cache.csv`、`data/split_adjustments.json` を自動コミットします。

- ワークフローには **`permissions: contents: write`** が必要（data/ の自動コミットのため）。
  リポジトリ設定 → Actions → General → Workflow permissions を **Read and write permissions** にしておくこと。
- ETF・REITのキャッシュ（`etf_dividends_cache.csv`）がコミットで永続化されることで、差分ローテーションが機能します。
- コミットメッセージに `[skip ci]` を付けて無限ループを防止しています。
- ETF・REITの取得件数は、ワークフローの `env.DIVIDEND_BATCH_SIZE` で上書きできます（既定200 / 空にすれば `config.yaml` の値）。
- 配当の配信リポジトリ（`sayonnsann/pharmacistlife-dividend-data`）は2つ目の `actions/checkout` で `edinet-feed/` に取得し、`env.FEED_DIR` 経由でスクリプトに渡しています。

### ディレクトリ構成の前提

このリポジトリは **リポジトリ直下＝プロジェクトルート**（`scripts/` や `data/` がリポジトリ直下にある）構成です。
- jsDelivr URL: `.../@main/data/database.csv`（サブディレクトリなし）
- ワークフロー（`update.yml`）も `env.PROJECT_DIR: "."` でこれに合わせています。

もし別リポジトリの `kouhaitou-db/` サブディレクトリに丸ごと置く場合は、`update.yml` の `env.PROJECT_DIR` を `kouhaitou-db` に、URL を `.../@main/kouhaitou-db/data/database.csv` に変更してください。

---

## Googleスプレッドシート側の手順（超重要）

### 1. 新DBスプレッドシートを作る

1. Googleドライブで**新規スプレッドシート**を作成。
2. シート名を **`データベース`** に変更（← この名前が重要。管理シートが `"データベース!A:S"` で参照するため）。
3. **A1セル**に次を入力（`<ユーザー名>` `<リポジトリ>` を自分のものに置換）：

   ```
   =IMPORTDATA("https://cdn.jsdelivr.net/gh/sayonnsann/kouhaitou-db@main/data/database.csv")
   ```

   → CSV全体が `A1:S…` に展開されます（A列＝コード … S列＝株価、S1＝最終更新日時）。

4. このスプレッドシートの**ID**を控える。URLの `/d/` と `/edit` の間の文字列です：
   `https://docs.google.com/spreadsheets/d/`**`ここがID`**`/edit`

### 2. 管理シート側を差し替える

1. 既存の**管理シート**を開く。
2. **`集計` シートの `P2` セル**の値を、**手順1で控えた新DBのID**に書き換える。
   （これで `IMPORTRANGE` の参照先が自作DBに切り替わります。）
3. 初回は `IMPORTRANGE` の**アクセス承認ダイアログ**が出るので「アクセスを許可」を押す。

これだけで、管理シート内のVLOOKUP・利回り計算・月別グラフ等がすべて自作DBを参照するようになります。

### jsDelivr のキャッシュについて

- jsDelivr は `@main` 指定だと**最大12時間**キャッシュされます。GitHub Actionsが早朝に更新しても、スプレッドシートへの反映が最大半日遅れる場合があります。
- すぐ反映したいときは、以下URLをブラウザで開いて**パージ（purge）**します：

  ```
  https://purge.jsdelivr.net/gh/sayonnsann/kouhaitou-db@main/data/database.csv
  ```

- `@main` の代わりにコミットハッシュ固定（`@<sha>`）にすると即時反映されますが、その都度URLを差し替える必要があります。通常運用では `@main` で十分です。
- スプレッドシート側で再取得したい場合は、A1の `IMPORTDATA` 式を一度消して再入力するか、ファイル→更新でキャッシュが切れます。

### 株価のみ更新（1日2回・`update-prices.yml`）

毎朝6:00の `update.yml`（フルビルド）とは別に、東証の市場セッションに合わせて
株価（S列）だけを1日2回更新する軽量ワークフローがあります。

| セッション | 目安時刻(JST) | 採用する値 | cron(UTC) |
|---|---|---|---|
| 前場寄付(`morning_open`) | 9:05頃 | 当日9:00の始値(Open) | `5 0 * * *` |
| 後場引け(`afternoon_close`) | 15:30頃 | 当日の終値(Close) | `30 6 * * *` |

- 実体は `scripts/update_prices_only.py`。既存 `data/database.csv` を読み、
  A列(銘柄コード)ごとに yfinance で株価だけ取得し直し、S列とヘッダの
  最終更新日時だけを差し替える（B〜R列＝銘柄名・業種・配当データは一切変更しない）。
- 前場寄付モードは「当日(JST)分の始値がまだ無い」銘柄（薄商い・未寄り付き・
  取得失敗）を前回値のまま保持する。後場引けモードは通常の終値取得と同じ挙動。
- 1銘柄も取得できなかった場合はCSVを書き換えずwarningで終了する（Actionsは失敗にしない）。
- `data/price_update_meta.json` に `{session, session_label, as_of_date, updated_at,
  updated_count, total_count}` を書き出す。19列のCSV契約は変えないまま、
  配信先が「前場寄付」「後場引け」どちらの株価かを区別できるようにするための補助ファイル。
- コミット後に jsDelivr の purge URL を叩き、即時反映を試みる（失敗しても非致命的）。
- `fetch_forecasts.py`（配当予想・edinetdb.jp APIの無料枠）はこのワークフローからは
  呼ばない。毎朝の `update.yml` のみが呼ぶので、1日2回動かしても枠を消費しない。

---

## 業種別ソート機能の追加（もう一つの不満点対応）

管理シートには業種で並べ替えたビューが無いため、空き領域か新規シートに `QUERY` 式でビューを作れます。データ元は管理シートの **`データ`** シート（No／コード／銘柄名／業種／…／配当金／利回り 等が並ぶ）。

### 業種→利回り降順で全件ソート

```
=QUERY('データ'!Q4:CA203, "select * where Col2 is not null order by <業種列> , <利回り列> desc", 1)
```

- `Q4:CA203` の範囲と `<業種列>`（`Col◯`）・`<利回り列>` は、実際の `データ` シートの見出しに合わせて調整してください。
- 業種は見出し **「　業種」** 列、利回りは **「利回り（時価）」** 列に対応します。`QUERY` の `Col◯` はレンジ先頭からの相対列番号（Q列＝Col1）で数えます。

### プルダウンで業種を選んで絞り込む版

任意のセル（例：`Z1`）に業種プルダウン（データの入力規則）を置き、選択値で絞り込みます：

```
=QUERY('データ'!Q4:CA203, "select * where Col<業種列> = '"&Z1&"' order by <利回り列> desc", 1)
```

`Z1` に「銀行業」等を選ぶと、その業種のみ利回り降順で表示されます。列番号は実シートに合わせて調整してください。

---

## 月別配当（G〜R列）の仮定について

- 各月列には**その月に支払われる1株配当額（円）**が入ります（既定 `value_mode: amount`）。年間配当Eを回数Fで等分し、支払月に配置します。
- 支払月は日本の慣行に基づき **権利確定月＋約3ヶ月**（例：3月末権利→6月、9月末権利→12月）をデフォルトにしています。この慣行は銘柄によって誤差があります。
- ズレが気になる場合は `config/config.yaml` の `month_basis` を `record`（権利確定月ベース）に、または `value_mode` を `flag`（1/0）に切り替え可能です。
- **調整方法**：既知の1銘柄（例：保有株）で、管理シートの月別配当グラフと本DBの月別列を突き合わせ、合うモードを選んでください。

---

## 配当データの中身（自前EDINET配信データ）

配当は公開リポジトリ [`sayonnsann/pharmacistlife-dividend-data`](https://github.com/sayonnsann/pharmacistlife-dividend-data) の `edinet/{4桁コード}.json` から読みます。金融庁EDINETの有価証券報告書をパースしたものです。feed側の株式分割・併合反映が遅れる期間は、本パイプラインが検証済み係数で現在の株数基準に揃えます。

このDBが使うキーは3つだけです。

| キー | 使い道 |
|------|--------|
| `dps` | `{年: 1株あたり年間配当}`。**最新年の値が E列（年間配当金）** |
| `dpsInterim` | `{年: 1株あたり中間配当}`。最新年が正なら **F列（回数）＝2**、そうでなければ 1 |
| `fiscalMonth` | 決算月。ここから権利確定月を推定し、G〜R列（月別）に配分 |

`dps` に値が無い銘柄（新規上場・無配・EDINET未収録）は、**E〜R列を空欄**にして行だけ出します（0では埋めません）。

### 実データで確認した値（2026-07-28 のビルド）

| コード | 銘柄 | E列（年間配当） | F列（回数） | 月別の配置 |
|--------|------|----------------|------------|-----------|
| 8058 | 三菱商事 | 110 円 | 2 | 6月 55 / 12月 55 |
| 7203 | トヨタ自動車 | 95 円 | 2 | 6月 47.5 / 12月 47.5 |
| 9432 | NTT | 5.3 円 | 2 | 6月 2.65 / 12月 2.65 |
| 1332 | ニッスイ | 32 円 | 2 | 6月 16 / 12月 16 |

いずれも3月決算・年2回なので、権利確定月（3月・9月）＋3ヶ月＝**6月と12月**に半分ずつ配置されます。

> 旧データ（配当予想ベース）と比べると、E列は**今期予想ではなく直近の実績**になります。増配発表の反映は有価証券報告書の提出後になるぶん遅くなりますが、値の出どころが一次情報になり、分割・併合の調整も入ります。

---

## 免責

- JPX の**ファイル構造が変わるとユニバース取得が失敗する**可能性があります（その場合はログに記録します）。
- 配当は**直近の実績値**です（今期予想ではありません）。月別配置は「権利確定月＋約3ヶ月」という慣行仮定に基づく推定値です。
- ETF・REITの分配金は yfinance の実績（直近1年）です。yfinance は非公式APIのため、取得できない銘柄・日はキャッシュの値が残ります。
- **投資判断はすべて自己責任です。** 本DBの数値の正確性は保証されません。必ず一次情報（各社IR・証券会社情報）で確認してください。

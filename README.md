# torecabank 買取リスト スクレイパー

[store.torecabank.com/kaitori_list](https://store.torecabank.com/kaitori_list) の全データを
毎日朝10時(日本時間)に自動取得し、Google スプレッドシートへ上書き保存します。

GitHub Actions で実行されるため、サーバーやPCを常時起動する必要はありません。

## 仕組み

- `scraper/scrape.py` … スクレイピング本体。ページネーションが JavaScript 駆動
  (`href="javascript:void(0)"`) のため、**Playwright** のヘッドレスブラウザで
  「次へ」をたどって全ページを巡回し、各商品から以下を抽出します。
  - 商品名 (`.name`)
  - グレード (`.tag` 例: PSA10)
  - 買取価格 (`.price` を数値化)
  - 在庫 (`.stock` 例: 残り2点 / 受付終了)
  - 受付状態 (募集中 / 受付終了 … `li.item.closed` から判定)
  - 画像URL (`.card img`)
- `.github/workflows/scrape.yml` … 毎日 01:00 UTC (= 10:00 JST) に実行する定期ジョブ。
- 取得結果は Google スプレッドシートの指定シートを **毎回クリアして上書き** します。
- `#itemCount`(例: 494件) を総件数として読み取り、取得漏れがあれば警告ログを出します。

## セットアップ手順

### 1. Google サービスアカウントを作成

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成 (既存でも可)。
2. 「APIとサービス」→「ライブラリ」で **Google Sheets API** を有効化。
3. 「APIとサービス」→「認証情報」→「認証情報を作成」→「サービスアカウント」を作成。
4. 作成したサービスアカウントの「キー」タブ →「鍵を追加」→「JSON」でキーを発行しダウンロード。
5. ダウンロードした JSON 内の `client_email` (例: `xxx@xxx.iam.gserviceaccount.com`) を控える。

### 2. スプレッドシートを準備

書き込み先は既定で以下に設定済みです (変更する場合は `scraper/scrape.py` の
`DEFAULT_SPREADSHEET_ID` / `DEFAULT_WORKSHEET_NAME`、または Secrets/Variables で上書き)。

- スプレッドシート: `1XZQO4j7gu-p9IsK3sfaQp4q9O2bH893C2PMv2h_xqzE`
- シート(タブ): `bank`

1. 上記スプレッドシートを、控えた **サービスアカウントのメールアドレスに「編集者」として共有**。
2. `bank` という名前のシート(タブ)を用意 (無い場合は自動作成されます)。

### 3. GitHub Secrets / Variables を登録

リポジトリの **Settings → Secrets and variables → Actions** で以下を登録します。

| 種別 | 名前 | 値 |
|------|------|----|
| Secret | `GOOGLE_SERVICE_ACCOUNT_JSON` | ダウンロードした JSON ファイルの**中身全体** (必須) |
| Secret (任意) | `SPREADSHEET_ID` | スプレッドシートID (未設定時は既定値を使用) |
| Variable (任意) | `WORKSHEET_NAME` | 書き込み先シート名 (既定: `bank`) |

> `GOOGLE_SERVICE_ACCOUNT_JSON` のみ必須です。`SPREADSHEET_ID` / `WORKSHEET_NAME` は
> 未設定なら `scraper/scrape.py` の既定値 (上記スプレッドシート / `bank` シート) が使われます。

### 4. 動作確認

**Actions** タブ →「買取リスト スクレイピング」→ **Run workflow** で手動実行できます。
`dry_run` に `1` を指定すると、スプレッドシートへ書き込まずログにサンプルを出力します。

## 任意の調整用 環境変数

| 環境変数 | 説明 |
|----------|------|
| `BASE_URL` | 取得対象URL (既定: 買取リストページ) |
| `MAX_PAGES` | ページ巡回の上限 (既定 100) |
| `REQUEST_DELAY` | ページ遷移後の待機秒 (既定 1.0) |
| `WORKSHEET_NAME` | 書き込み先シート名 (既定: 買取リスト) |
| `DRY_RUN` | `1` で書き込みせず動作確認 |

取得に失敗した場合、ジョブは最後に取得した HTML を `debug-page` という名前の
**Artifact** としてアップロードします。サイトのHTML構造が変わった場合は
これをダウンロードして確認し、`scraper/scrape.py` のセレクタを調整してください。

## ローカル実行 (任意)

```bash
pip install -r scraper/requirements.txt
python -m playwright install chromium
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service_account.json)"
export SPREADSHEET_ID="..."
python scraper/scrape.py            # 書き込みあり
DRY_RUN=1 python scraper/scrape.py  # 書き込みなし(動作確認)
```

## テスト

実際のページHTMLを `scraper/tests/fixture_page.html` に保存しており、
パーサーの抽出ロジックを検証できます。

```bash
python scraper/tests/test_parse.py
```

## みんなのポケカ相場：1〜3か月市場分析

`scrape_pokeca.yml` の `analysis` モード、または毎日の `daily` モードで、
PSA10の1〜3か月市場分析をスプレッドシートへ出力します。

| シート | 用途 |
|---|---|
| `pkc_analysis_requests` | 分析対象。`card_id`、カード名、番号、収録商品、基準日、URLを入力 |
| `pkc_psa_supply` | PSA Population Reportの時点履歴。日付、PSA10枚数、全グレード枚数、根拠URLを入力 |
| `pkc_liquidity` | 成約 (`sale`) と出品数スナップショット (`listing_snapshot`) を根拠URL付きで入力 |
| `pkc_market_analysis` | 価格・供給・流動性・スコア・1か月/3か月予測の銘柄別サマリー |
| `pkc_analysis_metrics` | 「指標／現在値／比較値／変化率／判定／取得元」の縦持ち表 |

価格履歴とPSA10市場指数は既存の `pkc_card_history` / `pkc_index_history` から
自動計算します。PSA枚数や外部成約が無い項目は推測せず `取得不能` と表示し、
取得済みの配点だけを100点換算します。データ取得率が低い場合は予測信頼度を
自動的に下げます。

`pkc_liquidity.record_type` は、成約行では `sale`、現在の出品数を記録する行では
`listing_snapshot` を指定します。同一出品・再出品と判断した行は
`is_duplicate=1` とし、成約件数から除外します。


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

1. 書き込み先の Google スプレッドシートを作成 (または既存を使用)。
2. そのスプレッドシートを、上で控えた **サービスアカウントのメールアドレスに「編集者」として共有**。
3. URL から **スプレッドシートID** を取得。
   `https://docs.google.com/spreadsheets/d/`**`<ここがID>`**`/edit`

### 3. GitHub Secrets / Variables を登録

リポジトリの **Settings → Secrets and variables → Actions** で以下を登録します。

| 種別 | 名前 | 値 |
|------|------|----|
| Secret | `GOOGLE_SERVICE_ACCOUNT_JSON` | ダウンロードした JSON ファイルの**中身全体** |
| Secret | `SPREADSHEET_ID` | スプレッドシートID |
| Variable (任意) | `WORKSHEET_NAME` | 書き込み先シート名 (既定: `買取リスト`) |

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

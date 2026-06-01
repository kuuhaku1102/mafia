# torecabank 買取リスト スクレイパー

[store.torecabank.com/kaitori_list](https://store.torecabank.com/kaitori_list) の全データを
毎日朝10時(日本時間)に自動取得し、Google スプレッドシートへ上書き保存します。

GitHub Actions で実行されるため、サーバーやPCを常時起動する必要はありません。

## 仕組み

- `scraper/scrape.py` … スクレイピング本体 (requests + BeautifulSoup)。
  価格(円/¥)を含む繰り返し要素を自動検出して、商品名・買取価格・画像URL・詳細URL を抽出します。
- `.github/workflows/scrape.yml` … 毎日 01:00 UTC (= 10:00 JST) に実行する定期ジョブ。
- 取得結果は Google スプレッドシートの指定シートを **毎回クリアして上書き** します。

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

## サイト構造に合わせた調整

セレクタは自動検出されますが、うまく取得できない場合はワークフローの `env:` に
以下を追加して明示指定できます (CSSセレクタ)。

| 環境変数 | 説明 |
|----------|------|
| `ITEM_SELECTOR` | 各商品を囲む要素 (例: `.item`) |
| `NAME_SELECTOR` | 商品名 (item内の相対セレクタ) |
| `PRICE_SELECTOR` | 買取価格 |
| `IMAGE_SELECTOR` | 画像 |
| `BASE_URL` | 取得対象URL |
| `MAX_PAGES` | ページネーション探索上限 (既定 300) |
| `REQUEST_DELAY` | リクエスト間隔秒 (既定 1.0) |

取得に失敗した場合、ジョブは最後に取得した HTML を `debug-page` という名前の
**Artifact** としてアップロードします。これをダウンロードして実際の構造を確認し、
上記セレクタを調整してください。

## ローカル実行 (任意)

```bash
pip install -r scraper/requirements.txt
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service_account.json)"
export SPREADSHEET_ID="..."
python scraper/scrape.py          # 書き込みあり
DRY_RUN=1 python scraper/scrape.py  # 書き込みなし(動作確認)
```

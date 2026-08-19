#!/usr/bin/env python3
"""
みんなのポケカ相場 (pokeca-chart.com) スクレイパー

用途は「2〜3日先に上がりそうか下がりそうかの傾向を出すこと」。
長期の価格予測ではない。買取価格を決めてから卸に下ろすまでの1〜2日のラグで
逆行するのを避けるのが目的なので、必要なのは「いくらになるか」ではなく
「今どちら向きに動いているか」の判定。運用に必要な履歴は直近20〜30日程度。

既存の torecabank (bank) / スニダン (snkr) スクレイパーとは
**完全に独立したプロセス** として動作する。
- scrape.py / snkr_scrape.py を import しない
- 環境変数はすべて PKC_ プレフィックス (共有は下記2つのみ)
- 書き込み先シートも別 (bank / snkr_map / snkr には一切触れない)

--------------------------------------------------------------------------
★既存スクレイパーの設計を真似してはいけない理由
--------------------------------------------------------------------------
既存 scrape.py は毎回 ws.clear() してから全書き換えしている。
これは「最新スナップショットだけ持つ」設計で、時系列が毎回消える。
今回の用途は傾向判定なので、この設計をコピーすると目的を達成できない。

このスクリプトは **追記型 (upsert)** で書き込む。ws.clear() は使わない。
同じ (date, index_type) / (date, card_id, condition) の行があれば更新、
無ければ追加する。1日2回走らせても行は重複しない。

--------------------------------------------------------------------------
★データ品質上の致命的な問題 (設計に反映済み)
--------------------------------------------------------------------------
サイトの説明ページによれば、取引が確認できない日は過去の直近取引価格を
引き継ぐ補完処理が入っている。つまり価格列だけでは
「価格が動いていない日」と「データが入力されなかった日」を区別できない。

これを除外せずに移動平均や連続日数を計算すると、
「5日間まったく取引が無かった薄い銘柄」が
「5日連続で価格が安定している優良銘柄」に見えてしまう。
移動平均は実態のない平坦な線になり、下落の立ち上がりを検知できない。

対策として trade_count (取引件数) を取得し、
trade_count == 0 または欠損の日は観測データとして扱わない
(平均にも streak にも含めない。前方補完もしない)。
trade_count が取得できない場合は「前日と価格が完全一致した日」を
補完候補とみなすフォールバックに切り替える (限界は下流に明示する)。

--------------------------------------------------------------------------
環境変数
--------------------------------------------------------------------------
既存と共有してよいもの (この2つだけ):
  GOOGLE_SERVICE_ACCOUNT_JSON  サービスアカウント鍵 (生JSON / base64 どちらも可)
  SPREADSHEET_ID               書き込み先スプレッドシートID

このスクリプト専用 (既存の WORKSHEET_NAME / REQUEST_DELAY / MAX_PAGES /
BASE_URL / SNKR_* は **絶対に読まない**):
  PKC_MODE              probe/backfill/index/cards/master/trend/daily (既定 daily)
  PKC_INDEX_WORKSHEET   指数の時系列シート名     (既定 pkc_index_history)
  PKC_CARD_WORKSHEET    カード別の時系列シート名 (既定 pkc_card_history)
  PKC_MASTER_WORKSHEET  カード一覧マスタ名       (既定 pkc_card_master)
  PKC_TREND_WORKSHEET   傾向フラグの出力先       (既定 pkc_trend)
  PKC_REQUEST_DELAY     リクエスト間隔秒 (既定 5.0 / これ未満には下げられない)
  PKC_MAX_ITEMS         処理件数の上限 0=無制限
  PKC_PROBE_URL         probe モードで開くURL
  PKC_SINCE             backfill の開始日 (YYYY-MM-DD)
  PKC_CONTACT           User-Agent に入れる連絡先 (相手先への配慮。設定推奨)

  DRY_RUN=1             スプレッドシートへ書き込まずログ出力のみ
"""

import csv
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

# ===========================================================================
# 【要設定】URL / セレクタ / JSONキー 設定ブロック
# ---------------------------------------------------------------------------
# pokeca-chart.com は Next.js 製で、HTMLを取っても価格は入っていない。
# DOM も API も未確認のため、ここの値はすべて「候補」である。
# PKC_MODE=probe で実ページとネットワークログを保存し、確定させること。
#
# 方針: まず JSON (APIレスポンス / 埋め込みJSON) から取ることを試みる。
#       ページを開いて DOM を舐めるより軽く・速く・安定する。
#       特に backfill では決定的な差になる。
# ===========================================================================

BASE = "https://pokeca-chart.com"

# --- 巡回対象URL (要設定: 実際のパスは probe で確認すること) ---------------
URLS = {
    "index": f"{BASE}/",
    "all_card": f"{BASE}/all-card/",
}

# --- 鑑定特化モード (PSA10 の価格・ランキングはこちら側に出る) -------------
# 通常モードと鑑定モードの両方を取得する。切り替え方法は未確認なので、
# URLクエリ候補とトグル要素候補の両方を用意する。
APPRAISAL_QUERY_CANDIDATES = ["?mode=psa", "?psa=1", "?appraisal=1"]
APPRAISAL_TOGGLE_SELECTORS = [
    "[class*='appraisal'] input[type='checkbox']",
    "[class*='psa'] input[type='checkbox']",
    "button:has-text('鑑定')",
    "label:has-text('鑑定')",
]

# --- 【要設定】APIエンドポイント候補 ---------------------------------------
# probe のネットワークログ (pkc_probe_network.json) で JSON を返している
# エンドポイントが見つかったら、ここに追記する。
# 見つかれば backfill が現実的になる。空のままでも daily は動く。
API_ENDPOINTS: list[str] = []

# --- JSON中の系列を見つけるためのキー候補 (要設定) -------------------------
# 実際のキー名は未確認。probe の結果を見て過不足を調整する。
DATE_KEYS = ["date", "day", "d", "x", "label", "tradeDate", "trade_date", "createdAt", "time"]
PRICE_KEYS = ["price", "value", "y", "avg", "average", "averagePrice", "median", "amount", "yen"]
COUNT_KEYS = [
    "tradeCount", "trade_count", "count", "dealCount", "deal_count",
    "volume", "num", "n", "transactions", "取引件数",
]

# --- DOM フォールバック用セレクタ候補 (要設定) -----------------------------
# JSON が取れなかった場合にだけ使う。
INDEX_VALUE_SELECTORS = [
    "[class*='index'] [class*='value']",
    "[class*='chartValue']",
    "[class*='price']",
]
CARD_LINK_SELECTORS = [
    "a[href*='/card/']",
    "a[href*='/cards/']",
    "[class*='cardList'] a[href]",
]
CARD_ROW_SELECTORS = [
    "table tbody tr",
    "[class*='cardList'] li",
    "[class*='row']",
]

# 描画完了待ちセレクタ候補 (要設定)
READY_SELECTORS = ["main", "[class*='chart']", "table", "body"]

# ===========================================================================
# 設定 (環境変数)
# ===========================================================================
DEFAULT_SPREADSHEET_ID = "1XZQO4j7gu-p9IsK3sfaQp4q9O2bH893C2PMv2h_xqzE"

MODE = (os.environ.get("PKC_MODE") or "daily").strip().lower()
MAX_ITEMS = int(os.environ.get("PKC_MAX_ITEMS") or "0")  # 0 = 無制限
PROBE_URL = (os.environ.get("PKC_PROBE_URL") or "").strip()
SINCE = (os.environ.get("PKC_SINCE") or "").strip()
CONTACT = (os.environ.get("PKC_CONTACT") or "").strip()

# ★相手先への配慮: 個人運営の無料ファンサイトなので、これ未満には下げられない。
MIN_REQUEST_DELAY = 5.0
REQUEST_DELAY = max(MIN_REQUEST_DELAY, float(os.environ.get("PKC_REQUEST_DELAY") or MIN_REQUEST_DELAY))

INDEX_WS = os.environ.get("PKC_INDEX_WORKSHEET") or "pkc_index_history"
CARD_WS = os.environ.get("PKC_CARD_WORKSHEET") or "pkc_card_history"
MASTER_WS = os.environ.get("PKC_MASTER_WORKSHEET") or "pkc_card_master"
TREND_WS = os.environ.get("PKC_TREND_WORKSHEET") or "pkc_trend"
WATCHLIST_WS = os.environ.get("PKC_WATCHLIST_WORKSHEET") or "pkc_watchlist"
STATUS_WS = os.environ.get("PKC_STATUS_WORKSHEET") or "pkc_card_status"
ANALYSIS_REQUEST_WS = os.environ.get("PKC_ANALYSIS_REQUEST_WORKSHEET") or "pkc_analysis_requests"
PSA_SUPPLY_WS = os.environ.get("PKC_PSA_SUPPLY_WORKSHEET") or "pkc_psa_supply"
LIQUIDITY_WS = os.environ.get("PKC_LIQUIDITY_WORKSHEET") or "pkc_liquidity"
ANALYSIS_WS = os.environ.get("PKC_ANALYSIS_WORKSHEET") or "pkc_market_analysis"
ANALYSIS_METRIC_WS = os.environ.get("PKC_ANALYSIS_METRIC_WORKSHEET") or "pkc_analysis_metrics"
# card モードで調べたい銘柄 (品番 / 名前 / card_id / URL のいずれか)
CARD_QUERY = (os.environ.get("PKC_CARD_QUERY") or "").strip()
# ウォッチリストが空のとき、全カードを巡回してよいか (既定: しない)
CRAWL_ALL = os.environ.get("PKC_CRAWL_ALL") == "1"
# 直近何日ぶんを card モードの明細表に出すか
SHOW_DAYS = int(os.environ.get("PKC_SHOW_DAYS") or "20")

INDEX_HEADERS = ["date", "index_type", "value", "diff", "diff_pct", "fetched_at"]
CARD_HEADERS = [
    "date", "card_id", "card_name", "hinban", "condition",
    "price", "trade_count", "imputed_suspect", "fetched_at",
]
MASTER_HEADERS = ["card_id", "card_name", "hinban", "url", "last_seen_at"]
# ★対象カードの指定用。品番と名前を手入力すれば、そのカードだけを巡回する。
# 5秒間隔・1日1回では全カードを回りきれないので、実運用ではこれで絞る。
WATCHLIST_HEADERS = ["品番", "名前", "card_id", "url", "メモ", "PSA10判定"]
# ★対象カードの現況ボード。毎朝これを見て買取価格を決める想定。
# 履歴は pkc_card_history / pkc_trend が持つので、こちらは
# 「1銘柄1行の最新状態」を上書き更新する view として扱う (行は増やさない)。
STATUS_HEADERS = [
    "品番", "名前", "状態", "傾向", "買取調整%",
    "最新価格", "最新日", "最新が補完",
    "d1%", "d3%", "d7%", "ma5", "ma20", "連続日数", "変動%",
    "有効観測日", "記録日数", "判定根拠", "直近1週間", "card_id", "更新日時",
]
TREND_HEADERS = [
    "date", "scope", "key", "label", "d1", "d3", "d7", "ma5", "ma20", "streak", "vol20",
    "valid_days", "trend", "suggested_buffer_pct", "imputation_basis", "fetched_at",
]
ANALYSIS_REQUEST_HEADERS = [
    "card_id", "カード名", "カード番号", "収録商品・プロモ名", "言語", "グレード",
    "分析基準日", "カードURL", "有効", "メモ",
]
PSA_SUPPLY_HEADERS = [
    "date", "card_id", "psa10_count", "all_grade_count", "source_url", "fetched_at",
]
LIQUIDITY_HEADERS = [
    "date", "card_id", "record_type", "price", "listing_id", "is_duplicate",
    "current_listings", "source_url", "fetched_at",
]
ANALYSIS_HEADERS = [
    "分析基準日", "card_id", "カード名", "カード番号", "収録商品", "言語", "グレード",
    "現在PSA10相場", "7日前", "30日前", "90日前", "180日前",
    "90日最高値", "最高値日", "90日最安値", "7日騰落率%", "30日騰落率%",
    "90日騰落率%", "最高値からの下落率%", "価格更新日", "直近取引日", "90日観測数",
    "未鑑定品相場", "PSA10価格差", "PSA10プレミアム倍率",
    "PSA10市場指数", "指数7日%", "指数30日%", "指数90日%", "市場相対強度",
    "PSA10枚数", "全グレード枚数", "PSA10率%", "PSA10_30日増加数", "PSA10_90日増加数",
    "PSA10_30日増加率%", "PSA10_90日増加率%",
    "30日成約件数", "90日成約件数", "直近成約日", "成約間隔中央値日",
    "直近成約価格中央値", "最高成約価格", "最低成約価格", "重複候補数",
    "現在出品数", "需給吸収率", "販売在庫月数",
    "価格トレンド点", "相対強度点", "流動性点", "供給リスク点", "価格バランス点",
    "相場強度スコア", "データ取得率%", "予測信頼度", "市場フェーズ",
    "1か月上昇%", "1か月横ばい%", "1か月下落%",
    "3か月上昇%", "3か月横ばい%", "3か月下落%",
    "強気価格", "基本価格", "弱気価格", "上昇要因", "下落要因",
    "最重要先行指標", "次回確認条件", "参照URL", "更新日時",
]
ANALYSIS_METRIC_HEADERS = [
    "分析基準日", "card_id", "カード名", "指標", "現在値", "比較値", "変化率",
    "判定", "取得元", "source_url", "更新日時",
]

# upsert のキー (これが一意性の定義)
INDEX_KEY_FIELDS = ["date", "index_type"]
CARD_KEY_FIELDS = ["date", "card_id", "condition"]
MASTER_KEY_FIELDS = ["card_id"]
WATCHLIST_KEY_FIELDS = ["品番", "名前"]
# 1銘柄(card_id)×状態(condition) で1行。日付をキーに含めないので行が増えない。
STATUS_KEY_FIELDS = ["card_id", "状態"]
TREND_KEY_FIELDS = ["date", "scope", "key"]
ANALYSIS_KEY_FIELDS = ["分析基準日", "card_id"]
ANALYSIS_METRIC_KEY_FIELDS = ["分析基準日", "card_id", "指標"]

# --- 傾向判定の閾値 -------------------------------------------------------
# ★非対称に倒すこと (依頼の要件)
#   下落を見逃す (実際は下がるのに横ばい判定) → 利幅が飛ぶ。高コスト
#   下落の空振り (実際は下がらないのに下落判定) → 少し安く買っただけ。低コスト
# よって下落側は検知しやすく、上昇側は厳しくする。
# 依頼書の初期値は上下対称 (±1% / ±2連続) だったが、
# 「下落側を緩く、上昇側を厳しく」という指示に従い既定値を非対称にしてある。
# 数値だけでなく条件の数も非対称: 下落は3条件中2つ、上昇は3条件すべて。
DOWN_D3 = float(os.environ.get("PKC_DOWN_D3") or "-0.8")     # 依頼書の -1.0 より緩い
DOWN_STREAK = int(os.environ.get("PKC_DOWN_STREAK") or "-2")
UP_D3 = float(os.environ.get("PKC_UP_D3") or "1.5")          # 依頼書の +1.0 より厳しい
UP_STREAK = int(os.environ.get("PKC_UP_STREAK") or "3")      # 依頼書の +2 より厳しい
# 依頼要件どおり、有効観測日が10日未満なら無理に方向を判定しない。
# PKC_MIN_VALID_DAYS で運用時に厳しくすることはできるが、10未満には下げない。
MIN_VALID_DAYS = max(10, int(os.environ.get("PKC_MIN_VALID_DAYS") or "10"))
BUFFER_MULTIPLIER = float(os.environ.get("PKC_BUFFER_MULTIPLIER") or "2.0")

# --- User-Agent ------------------------------------------------------------
# ★相手先への配慮: 連絡先を含める。PKC_CONTACT が未設定なら警告する
#   (勝手にメールアドレスを埋め込むことはしない)。
if CONTACT:
    USER_AGENT = (
        f"pokeca-chart-scraper/1.0 (+https://github.com/; contact: {CONTACT})"
    )
else:
    USER_AGENT = "pokeca-chart-scraper/1.0 (+https://github.com/)"


def log(msg: str) -> None:
    print(msg, flush=True)


def now_jst() -> datetime:
    return datetime.now(ZoneInfo("Asia/Tokyo"))


def today_jst() -> str:
    return now_jst().strftime("%Y-%m-%d")


def timestamp_jst() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# robots.txt (相手先への配慮 / 必須)
# ===========================================================================
def fetch_robots(base: str = BASE) -> str | None:
    """robots.txt を取得する。取得できなければ None。"""
    import urllib.request

    url = urljoin(base, "/robots.txt")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log(f"! robots.txt を取得できませんでした ({exc})")
        return None


def robots_allows(robots_txt: str | None, path: str, agent: str = "*") -> bool | None:
    """robots.txt が path の取得を許可しているか。

    True=許可 / False=禁止 / None=判定不能 (robots.txt を取得できなかった)。
    自分の UA 向けの記述があればそれを優先し、無ければ * を見る。
    """
    if robots_txt is None:
        return None

    from urllib.robotparser import RobotFileParser

    rp = RobotFileParser()
    rp.parse(robots_txt.splitlines())
    # 自分の UA 名 (先頭トークン) でも * でも確認し、どちらかが禁止なら禁止扱い
    ua_token = USER_AGENT.split("/")[0]
    for ua in (ua_token, agent):
        try:
            if not rp.can_fetch(ua, path):
                return False
        except Exception:
            continue
    return True


def assert_robots_allowed(urls: list[str]) -> bool:
    """巡回対象が robots.txt で許可されているか確認する。

    禁止されていたらログに明示して False を返す (呼び出し側で終了する)。
    """
    robots = fetch_robots()
    if robots is None:
        log("! robots.txt を確認できませんでした。取得を続行しますが、")
        log("  相手先は個人運営の無料ファンサイトです。手動で robots.txt を確認してください。")
        return True

    log("--- robots.txt の確認 ---")
    ok = True
    for u in urls:
        path = urlparse(u).path or "/"
        allowed = robots_allows(robots, path)
        log(f"  {'許可' if allowed else '禁止'}: {path}")
        if allowed is False:
            ok = False
    if not ok:
        log("! robots.txt で禁止されている対象があります。取得せずに終了します。")
    return ok


# ===========================================================================
# パース (純粋関数 / テスト対象)
# ===========================================================================
# 全角・カンマ・円記号・符号のゆれを吸収する。
_MINUS_CHARS = "-−－‐‑–—―▲△"  # ▲△ は日本語表記で負を表す慣習


def parse_price(text) -> int | None:
    """価格文字列を整数にする。

      "152,443円" -> 152443
      "¥152,443"  -> 152443
      "１５２，４４３"(全角) -> 152443
      "-" / "" / None -> None (欠損。0 と区別する)
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    t = unicodedata.normalize("NFKC", str(text)).strip()
    if not t:
        return None
    neg = bool(re.match(r"^\s*[" + re.escape(_MINUS_CHARS) + r"]", t))
    digits = re.sub(r"[^0-9]", "", t)
    if not digits:
        return None
    v = int(digits)
    return -v if neg else v


def parse_pct(text) -> float | None:
    """パーセント文字列を float にする ("+0.75%" -> 0.75)。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = unicodedata.normalize("NFKC", str(text)).strip()
    m = re.search(r"([" + re.escape(_MINUS_CHARS) + r"+]?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%", t)
    if not m:
        return None
    v = float(m.group(2).replace(",", ""))
    return -v if m.group(1) and m.group(1) in _MINUS_CHARS else v


def parse_diff(text) -> tuple[int | None, float | None]:
    """前日比の文字列を (差額, 変化率%) にする。

      "+1,138円(+0.75%)" -> (1138, 0.75)
      "-1,138円(-0.75%)" -> (-1138, -0.75)
      "▲1,138円(▲0.75%)" -> (-1138, -0.75)   (▲△ は負を表す慣習)
      "±0円(0.00%)"      -> (0, 0.0)
    """
    if text is None:
        return None, None
    t = unicodedata.normalize("NFKC", str(text)).strip()
    if not t:
        return None, None

    pct = parse_pct(t)

    # 金額は「円」が付いている数値を優先。無ければ % 以外の最初の数値。
    amount = None
    m = re.search(r"([" + re.escape(_MINUS_CHARS) + r"+±]?)\s*([0-9][0-9,]*)\s*円", t)
    if not m:
        # 括弧より前の部分から数値を探す (括弧内は % のことが多い)
        head = t.split("(")[0]
        m = re.search(r"([" + re.escape(_MINUS_CHARS) + r"+±]?)\s*([0-9][0-9,]*)", head)
    if m:
        amount = int(m.group(2).replace(",", ""))
        if m.group(1) and m.group(1) in _MINUS_CHARS:
            amount = -amount
    return amount, pct


def parse_date(value, default_year: int | None = None) -> str:
    """日付を YYYY-MM-DD に正規化する。

      "2026/08/18" / "2026-08-18" / "2026.08.18" -> "2026-08-18"
      "8月18日" -> default_year を使って "YYYY-08-18" (未指定なら空)
      epoch秒 / epochミリ秒 の数値もJSTの日付に変換する。
    """
    if value is None or value == "":
        return ""

    # epoch (数値) の場合
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v > 1e11:  # ミリ秒
            v /= 1000.0
        if v > 1e8:  # 1973年以降なら epoch とみなす
            return datetime.fromtimestamp(v, ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
        return ""

    t = unicodedata.normalize("NFKC", str(value)).strip()
    m = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\s*[-/.月]\s*(\d{1,2})", t)
        if not m or default_year is None:
            return ""
        y, mo, d = default_year, int(m.group(1)), int(m.group(2))
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def slug_from_url(url: str) -> str:
    """カードURLから card_id (スラッグ) を取り出す。

      "https://pokeca-chart.com/card/abc-123/" -> "abc-123"
    """
    path = urlparse((url or "").strip()).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


# ===========================================================================
# JSON からの系列抽出
# ---------------------------------------------------------------------------
# Next.js なので、価格は HTML ではなく JSON (APIレスポンス / __NEXT_DATA__ /
# RSCペイロード) 側にある。キー名が未確認でも拾えるよう、
# 「日付らしいキー」と「価格らしいキー」を持つ辞書の配列を総当たりで探す。
# ===========================================================================
def _match_key(d: dict, candidates: list[str]) -> str:
    """辞書のキーの中から候補に合うものを返す (大文字小文字・記号を無視)。"""
    norm = {re.sub(r"[_\-\s]", "", k).lower(): k for k in d.keys() if isinstance(k, str)}
    for c in candidates:
        k = re.sub(r"[_\-\s]", "", c).lower()
        if k in norm:
            return norm[k]
    return ""


def iter_json_values(text: str, max_scans: int = 20000):
    """テキスト中に埋め込まれた JSON 値を片っ端から取り出す。

    RSCペイロードのように JSON 断片が地の文に混ざっている場合に使う。
    '{' か '[' の位置ごとに raw_decode を試す。
    """
    decoder = json.JSONDecoder()
    n = len(text or "")
    i = 0
    scans = 0
    while i < n and scans < max_scans:
        ch = text[i]
        if ch not in "[{":
            i += 1
            continue
        scans += 1
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        yield obj
        i = max(end, i + 1)


def find_json_blobs(html: str) -> list:
    """HTML から埋め込みJSONを取り出す。

    __NEXT_DATA__ / application/json / ld+json と、
    Next.js App Router の self.__next_f.push([1,"..."]) を対象にする。
    """
    blobs = []
    if not html:
        return blobs

    for m in re.finditer(
        r'<script[^>]*(?:id="__NEXT_DATA__"|type="application/(?:json|ld\+json)")[^>]*>(.*?)</script>',
        html, re.S,
    ):
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            continue

    # RSC ペイロード: JSON文字列としてエスケープされているので一度デコードする
    for m in re.finditer(r'self\.__next_f\.push\(\s*\[\s*\d+\s*,\s*("(?:[^"\\]|\\.)*")\s*\]\s*\)', html):
        try:
            chunk = json.loads(m.group(1))
        except Exception:
            continue
        blobs.extend(iter_json_values(chunk))
    return blobs


def normalize_series(raw_list: list) -> list[dict]:
    """辞書の配列を {date, price, trade_count} の系列に正規化する。

    trade_count が無ければ None (欠損) を入れる。None と 0 は区別する。
    """
    out = []
    for item in raw_list or []:
        if not isinstance(item, dict):
            continue
        dk = _match_key(item, DATE_KEYS)
        pk = _match_key(item, PRICE_KEYS)
        if not dk or not pk:
            continue
        date = parse_date(item.get(dk))
        price = parse_price(item.get(pk))
        if not date or price is None:
            continue
        ck = _match_key(item, COUNT_KEYS)
        count = None
        if ck:
            c = item.get(ck)
            count = parse_price(c) if c is not None else None
        out.append({"date": date, "price": price, "trade_count": count})
    return out


def walk_series(obj, path: str = "", depth: int = 0, max_depth: int = 12) -> list[tuple[str, list[dict]]]:
    """JSON を再帰的に歩き、時系列らしい配列をすべて拾う。

    返り値は (JSON内のパス, 正規化済み系列) のリスト。
    パスを返すのは、probe で「どのキーに入っていたか」を人間に見せるため。
    """
    found = []
    if depth > max_depth:
        return found

    if isinstance(obj, list):
        series = normalize_series(obj)
        # 2点以上あって初めて時系列とみなす (単発のオブジェクト配列を除外)
        if len(series) >= 2:
            found.append((path or "$", series))
        else:
            for i, v in enumerate(obj[:50]):
                found.extend(walk_series(v, f"{path}[{i}]", depth + 1, max_depth))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found.extend(walk_series(v, f"{path}.{k}" if path else str(k), depth + 1, max_depth))
    return found


def has_trade_count(series: list[dict]) -> bool:
    """系列に取引件数が入っているか (1つでも非Noneがあれば True)。"""
    return any(r.get("trade_count") is not None for r in series or [])


# ===========================================================================
# ★傾向判定ロジック (このスクレイパーの出力の本体)
# ---------------------------------------------------------------------------
# 機械学習は使わない。移動平均と変化率で十分。
#
# ★★★ 必ず踏む罠: 補完日を計算から除外すること ★★★
# サイトは取引が無い日に前日価格を引き継ぐ。これを除外せずに streak や
# 移動平均を計算すると、「5日間まったく取引が無かった薄い銘柄」が
# 「5日連続で価格が安定している優良銘柄」に見える。移動平均は実態のない
# 平坦な線になり、下落の立ち上がりを検知できない。
# ===========================================================================
def mark_imputed(series: list[dict]) -> tuple[list[dict], str]:
    """各行に「補完の疑い」フラグを立てる。

    判定根拠 (imputation_basis) は2通り:
      "trade_count"  … 取引件数が取れている場合。件数0/欠損の日を補完とみなす
      "price_repeat" … 取引件数が取れない場合のフォールバック。
                       前日と価格が完全一致した日を補完候補とみなす
                       (実際に値動きが無かっただけの日も巻き込む。精度は落ちる)
    返り値は (フラグ付き系列, 判定根拠)。系列は日付昇順・同日は後勝ちで重複排除。
    """
    # 日付昇順に整列し、同じ日付が複数あれば後のものを採用
    by_date = {}
    for r in series or []:
        if r.get("date"):
            by_date[r["date"]] = dict(r)
    rows = [by_date[d] for d in sorted(by_date)]

    basis = "trade_count" if has_trade_count(rows) else ("price_repeat" if rows else "none")

    prev_price = None
    for r in rows:
        tc = r.get("trade_count")
        if basis == "trade_count":
            # 件数が欠損 (None) の日も実測とみなせないので補完扱いにする
            r["imputed_suspect"] = tc is None or tc <= 0
        else:
            r["imputed_suspect"] = prev_price is not None and r.get("price") == prev_price
        prev_price = r.get("price")
    return rows, basis


def _pct_change(new: float, old: float) -> float | None:
    if old in (None, 0) or new is None:
        return None
    return (new / old - 1.0) * 100.0


def compute_metrics(series: list[dict]) -> dict:
    """補完日を除外したうえで d1 / d3 / ma5 / ma20 / streak / vol20 を計算する。

    除外は「その日は無かったこと」にする。前方補完はしない。
    """
    rows, basis = mark_imputed(series)
    valid = [r for r in rows if not r["imputed_suspect"] and r.get("price") is not None]
    prices = [float(r["price"]) for r in valid]

    out = {
        "d1": None, "d3": None, "d7": None, "ma5": None, "ma20": None,
        "streak": 0, "vol20": None,
        "valid_days": len(valid),
        "imputation_basis": basis,
        "last_date": valid[-1]["date"] if valid else "",
    }
    if len(prices) < 2:
        return out

    # 日次変化率 (有効観測日の連続として計算する。カレンダー日ではない)
    changes = [
        _pct_change(prices[i], prices[i - 1]) for i in range(1, len(prices))
    ]
    changes = [c for c in changes if c is not None]

    out["d1"] = changes[-1] if changes else None
    if len(prices) >= 4:
        out["d3"] = _pct_change(prices[-1], prices[-4])
    # d7 = 直近7観測ぶんの変化率 (カレンダー7日ではなく「有効観測7日」の幅)。
    # 補完日を除いているので、薄い銘柄では実時間で2週間以上になることがある。
    if len(prices) >= 7:
        out["d7"] = _pct_change(prices[-1], prices[-7])

    if len(prices) >= 5:
        out["ma5"] = sum(prices[-5:]) / 5.0
    # ma20 は20日分揃っていなければ「あるだけ」で計算する。
    # MIN_VALID_DAYS 未満は判定不可にするので、極端に少ない本数では使われない。
    out["ma20"] = sum(prices[-20:]) / float(len(prices[-20:]))

    # streak: 直近から同じ向きが続いた日数 (下落なら負)
    streak = 0
    for c in reversed(changes):
        sign = 1 if c > 0 else (-1 if c < 0 else 0)
        if sign == 0:
            break
        if streak == 0 or (streak > 0 and sign > 0) or (streak < 0 and sign < 0):
            streak += sign
        else:
            break
    out["streak"] = streak

    recent = changes[-20:]
    if len(recent) >= 2:
        out["vol20"] = statistics.pstdev(recent)

    # 下落バッファの目安: 直近の下落日の平均下落率 × 係数
    downs = [abs(c) for c in recent if c < 0]
    out["_avg_down"] = (sum(downs) / len(downs)) if downs else None
    return out


def classify_trend(m: dict) -> str:
    """指標から3値 + 判定不可の傾向フラグを出す。

    ★非対称:
      下落 … 3条件のうち d3 は必須、残り (ma5<ma20 / streak) は **どちらか1つ** で成立
      上昇 … 3条件すべてを満たす必要がある
    下落を見逃すコストが高く、空振りのコストは低いため、意図的にこうしている。
    """
    if m.get("valid_days", 0) < MIN_VALID_DAYS:
        return "判定不可"

    d3 = m.get("d3")
    ma5, ma20 = m.get("ma5"), m.get("ma20")
    streak = m.get("streak", 0)

    if d3 is None:
        return "判定不可"

    ma_down = ma5 is not None and ma20 is not None and ma5 < ma20
    ma_up = ma5 is not None and ma20 is not None and ma5 > ma20

    # 下落判定: 緩め (d3 + いずれか1つ)
    if d3 < DOWN_D3 and (ma_down or streak <= DOWN_STREAK):
        return "下落"
    # 上昇判定: 厳しめ (3条件すべて)
    if d3 > UP_D3 and ma_up and streak >= UP_STREAK:
        return "上昇"
    return "横ばい"


def suggested_buffer_pct(m: dict, trend: str) -> str:
    """下落時のみ、買取表の利率調整に使うバッファの目安を返す。

    2 × 平均日次下落率。上昇・横ばい・判定不可では空にする。
    """
    if trend != "下落":
        return ""
    avg_down = m.get("_avg_down")
    if not avg_down:
        return ""
    return f"{round(avg_down * BUFFER_MULTIPLIER, 1)}"


def _fmt(v, digits: int = 2) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return ""
        return f"{round(v, digits)}"
    return str(v)


def build_trend_row(scope: str, key: str, series: list[dict], date: str = "",
                    label: str = "") -> dict:
    """1銘柄 (またはグループ・指数) 分の傾向行を作る。

    key は upsert の一意キーなので機械的な値 (card_id|condition) を保つ。
    人間が読むための名前は label 列に別で入れる (key を人間向けにすると
    カード名が変わった瞬間に別行として増えてしまうため)。
    """
    m = compute_metrics(series)
    trend = classify_trend(m)
    return {
        "date": date or m.get("last_date") or today_jst(),
        "scope": scope,
        "key": key,
        "label": label or key,
        "d1": _fmt(m["d1"]),
        "d3": _fmt(m["d3"]),
        "d7": _fmt(m["d7"]),
        "ma5": _fmt(m["ma5"], 1),
        "ma20": _fmt(m["ma20"], 1),
        "streak": str(m["streak"]),
        "vol20": _fmt(m["vol20"]),
        "valid_days": str(m["valid_days"]),
        "trend": trend,
        "suggested_buffer_pct": suggested_buffer_pct(m, trend),
        "imputation_basis": m["imputation_basis"],
        "fetched_at": timestamp_jst(),
    }


def group_series(rows: list[dict], group_of) -> dict:
    """カード別の履歴をグループ単位の系列に集約する。

    ★個別カードは取引が薄く、1〜2件の取引で数字が跳ねる。
    シリーズ / レアリティ帯などで束ねた方が安定し、実用的。

    集約は「同じ日付のカード価格の平均」。補完日は集約前に除外する
    (補完日を混ぜると、その銘柄の古い価格が平均を引きずるため)。
    group_of は行を受け取ってグループ名を返す関数。空文字を返した行は捨てる。
    """
    buckets: dict[str, dict[str, list[float]]] = {}
    counts: dict[str, dict[str, int]] = {}
    for r in rows or []:
        g = group_of(r)
        if not g or not r.get("date") or r.get("price") is None:
            continue
        tc = r.get("trade_count")
        # 補完日は集約に入れない
        if tc is not None and tc <= 0:
            continue
        buckets.setdefault(g, {}).setdefault(r["date"], []).append(float(r["price"]))
        counts.setdefault(g, {}).setdefault(r["date"], 0)
        counts[g][r["date"]] += int(tc) if tc is not None else 1

    out = {}
    for g, by_date in buckets.items():
        out[g] = [
            {
                "date": d,
                "price": sum(by_date[d]) / len(by_date[d]),
                # グループの取引件数は構成カードの合計。
                # 束ねることで「その日に何も観測されなかった」判定が安定する。
                "trade_count": counts[g][d],
            }
            for d in sorted(by_date)
        ]
    return out


# ===========================================================================
# 銘柄の名寄せと検索 (対象カードを指定・照会するため)
# ---------------------------------------------------------------------------
# 買取表側の品番・名前には表記ゆれが混ざる。マスタと突き合わせる前に正規化する。
# ★ハイフン文字セットは品番用と名前用で分ける。
#   品番の "SMｰP" は NFKC で長音「ー」になるので '-' に寄せる必要があるが、
#   同じ変換を名前に当てると「ルイージ」「ブラッキー」が壊れる。
# ===========================================================================
_COMMON_HYPHENS = "-­‐‑‒–—―⁃−－"          # 名前にも当ててよいハイフン類
_CODE_HYPHENS = _COMMON_HYPHENS + "ーｰ"    # 品番のみ (品番にカタカナは出ない)
_CODE_HYPHEN_RE = re.compile("[" + re.escape(_CODE_HYPHENS) + "]")
_NAME_HYPHEN_RE = re.compile("[" + re.escape(_COMMON_HYPHENS) + "]")
_SPACE_RE = re.compile(r"[\s　 ]+")


def normalize_hinban(raw: str) -> str:
    """品番を正規化する ("006/165 " -> "006/165", "211/SMｰP" -> "211/SM-P")。"""
    t = unicodedata.normalize("NFKC", raw or "")
    t = _CODE_HYPHEN_RE.sub("-", t)
    t = _SPACE_RE.sub("", t)
    return t.upper()


def normalize_name(raw: str) -> str:
    """カード名を正規化する。長音記号は絶対に '-' へ変えない。"""
    t = unicodedata.normalize("NFKC", raw or "")
    t = _NAME_HYPHEN_RE.sub("-", t)
    t = _SPACE_RE.sub(" ", t)
    return t.strip()


def compact_name(raw: str) -> str:
    """部分一致用に空白・記号・サイト側の品番括弧を除く。"""
    t = normalize_name(raw).lower()
    t = re.sub(r"\[[^\]]*\]", "", t)
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠々]+", "", t)


def names_partially_match(left: str, right: str) -> bool:
    a, b = compact_name(left), compact_name(right)
    return bool(a and b and (a in b or b in a))


def match_cards(query: str, cards: list[dict]) -> list[dict]:
    """品番 / 名前 / card_id / URL のどれでもカードを引けるようにする。

    一致の強い順に並べて返す (完全一致 → 前方一致 → 部分一致)。
    候補が複数出ることは普通にあるので、絞り込まず全部返して人間に選ばせる。
    """
    q = (query or "").strip()
    if not q:
        return []
    q_hin = normalize_hinban(q)
    q_name = normalize_name(q).lower()
    q_slug = slug_from_url(q) if "/" in q and "://" in q else ""

    scored = []
    for c in cards or []:
        cid = (c.get("card_id") or "").strip()
        hin = normalize_hinban(c.get("hinban"))
        name = normalize_name(c.get("card_name")).lower()

        score = 0
        if q_slug and cid == q_slug:
            score = 100
        elif cid and cid.lower() == q.lower():
            score = 100
        elif hin and q_hin and hin == q_hin:
            score = 90
        elif name and name == q_name:
            score = 80
        elif name and q_name and name.startswith(q_name):
            score = 60
        elif name and q_name and q_name in name:
            score = 40
        elif hin and q_hin and q_hin in hin:
            score = 30

        if score:
            scored.append((score, c))

    scored.sort(key=lambda x: (-x[0], x[1].get("card_name") or ""))
    return [c for _, c in scored]


def load_watchlist(sh) -> list[dict]:
    """対象カードの一覧を読む (品番・名前は人間が手入力)。"""
    rows = read_history(sh, WATCHLIST_WS, WATCHLIST_HEADERS)
    rows = [r for r in rows if (r.get("品番") or r.get("名前") or r.get("card_id"))]
    if rows:
        return rows

    # 初回だけ、依頼者提供の対象一覧を空のpkc_watchlistへ投入する。
    seed_path = os.path.join(os.path.dirname(__file__), "pokeca_watchlist.tsv")
    if not os.path.exists(seed_path):
        return []
    with open(seed_path, encoding="utf-8", newline="") as f:
        seeded = [dict(r) for r in csv.DictReader(f, delimiter="\t")]
    seeded = [r for r in seeded if r.get("品番") or r.get("名前")]
    log(f"  空の{WATCHLIST_WS}に初期ウォッチリスト {len(seeded)}件を読み込みます。")
    return seeded


def resolve_watchlist(sh, watch: list[dict], master: list[dict]) -> list[dict]:
    """ウォッチリストの各行にマスタから card_id / url を埋める。

    既に card_id が入っている行は触らない (人間が手で直した値を尊重する)。
    """
    resolved = []
    for r in watch:
        row = dict(r)
        if not row.get("card_id"):
            q = row.get("品番") or row.get("名前") or ""
            hits = match_cards(q, master)
            # 品番と名前の両方があるなら、両方で絞り込んで精度を上げる
            if row.get("品番") and row.get("名前"):
                both = [c for c in match_cards(row["品番"], master)
                        if names_partially_match(row["名前"], c.get("card_name"))]
                hits = both or hits
            if hits:
                row["card_id"] = hits[0].get("card_id", "")
                row["url"] = hits[0].get("url", "")
                if len(hits) > 1:
                    row["メモ"] = f"候補{len(hits)}件から先頭を採用（要確認）"
                log(f"  照合: {q} -> {hits[0].get('card_name')} ({row['card_id']})")
            else:
                row["メモ"] = "マスタに見つかりません（手でcard_id/urlを入れてください）"
                log(f"  ! 照合できませんでした: {q}")
        resolved.append(row)
    return resolved


# ===========================================================================
# card モード: 対象カードが今どうなっているかを照会する
# ---------------------------------------------------------------------------
# サイトへはアクセスしない。蓄積済みの履歴だけで答えるので何度でも実行できる。
# ===========================================================================
def _trend_mark(trend: str) -> str:
    return {"下落": "▼下落", "上昇": "▲上昇", "横ばい": "→横ばい"}.get(trend, "?判定不可")


def report_card(card_rows: list[dict], query: str, show_days: int = 20) -> int:
    """1銘柄の状態をログに出す。戻り値は見つかった系列の数。"""
    cards = []
    seen = set()
    for r in card_rows:
        cid = r.get("card_id", "")
        if cid and cid not in seen:
            seen.add(cid)
            cards.append({
                "card_id": cid,
                "card_name": r.get("card_name", ""),
                "hinban": r.get("hinban", ""),
            })

    hits = match_cards(query, cards)
    if not hits:
        log(f"! '{query}' に一致するカードが履歴にありません。")
        log("  pkc_card_master / pkc_card_history に入っているか確認してください。")
        return 0

    if len(hits) > 1:
        log(f"'{query}' の候補が {len(hits)} 件あります。すべて表示します。")

    shown = 0
    for card in hits[:5]:
        cid = card["card_id"]
        rows = [r for r in card_rows if r.get("card_id") == cid]
        conditions = sorted({r.get("condition", "") for r in rows})

        log("")
        log("=" * 72)
        log(f" {card.get('card_name') or cid}  [{card.get('hinban') or '品番不明'}]  ({cid})")
        log("=" * 72)

        for cond in conditions:
            crows = [r for r in rows if r.get("condition", "") == cond]
            series = _to_series(crows)
            if not series:
                continue
            shown += 1
            m = compute_metrics(series)
            trend = classify_trend(m)
            marked, basis = mark_imputed(series)
            latest = marked[-1] if marked else {}

            log("")
            log(f"--- 状態: {cond or '不明'} ---")
            log(f"  傾向        : {_trend_mark(trend)}")
            log(f"  最新価格    : {latest.get('price', '-')} 円 ({latest.get('date', '-')})"
                + ("  ※この日は取引なし（補完値）" if latest.get("imputed_suspect") else ""))
            log(f"  1日変化 d1  : {_fmt(m['d1'])}%")
            log(f"  3日変化 d3  : {_fmt(m['d3'])}%")
            log(f"  1週間変化d7 : {_fmt(m['d7'])}%")
            log(f"  ma5 / ma20  : {_fmt(m['ma5'], 1)} / {_fmt(m['ma20'], 1)}")
            log(f"  連続日数    : {m['streak']}  (負なら下落が続いている)")
            log(f"  変動の大きさ: {_fmt(m['vol20'])}%")
            log(f"  有効観測日  : {m['valid_days']}日 / 記録{len(marked)}日"
                f"  (判定には{MIN_VALID_DAYS}日必要)")
            log(f"  判定根拠    : {basis}"
                + ("  ※取引件数が無いため精度が落ちます" if basis == "price_repeat" else ""))
            if trend == "下落":
                buf = suggested_buffer_pct(m, trend)
                log(f"  ★買取率から {buf}% ほど引くことを検討してください")
            elif trend == "判定不可":
                log("  ★実測が足りません。いつもより慎重に。")

            # 直近の明細。補完日が一目で分かるようにする。
            log(f"  直近{show_days}日:")
            log(f"    {'日付':<12}{'価格':>10}{'取引':>6}  備考")
            for r in marked[-show_days:]:
                tc = r.get("trade_count")
                note = "補完(取引なし)" if r.get("imputed_suspect") else ""
                log(f"    {r['date']:<12}{r['price']:>10}{('-' if tc is None else tc):>6}  {note}")
    return shown


# ===========================================================================
# 現況ボード (対象カードの最新状態をシートへ転記する)
# ---------------------------------------------------------------------------
# ログだけだと毎朝見るのに不便なので、1銘柄1行の表としてシートに出す。
# 履歴は pkc_card_history / pkc_trend が持っているので、
# こちらは「最新状態の view」として上書き更新する (行は増やさない)。
# ===========================================================================
def price_trail(marked: list[dict], n: int = 7) -> str:
    """直近の価格推移を1セルに収める ("139000→137800*→136000")。

    末尾に * が付いている日は補完 (取引が無く前日価格を引き継いだ日)。
    セル1つで「本当に動いていないのか、誰も取引していないだけか」が見える。
    """
    parts = []
    for r in (marked or [])[-n:]:
        v = r.get("price")
        if v is None:
            continue
        parts.append(f"{int(v)}{'*' if r.get('imputed_suspect') else ''}")
    return "→".join(parts)


def build_status_row(card: dict, condition: str, series: list[dict]) -> dict:
    """1銘柄×1状態 の現況行を作る。"""
    m = compute_metrics(series)
    trend = classify_trend(m)
    marked, basis = mark_imputed(series)
    latest = marked[-1] if marked else {}
    return {
        "品番": card.get("hinban", ""),
        "名前": card.get("card_name", ""),
        "状態": condition or "不明",
        "傾向": trend,
        "買取調整%": suggested_buffer_pct(m, trend),
        "最新価格": latest.get("price", ""),
        "最新日": latest.get("date", ""),
        # 最新日が補完だと、その価格は実測ではない。必ず見えるようにする。
        "最新が補完": "★補完" if latest.get("imputed_suspect") else "",
        "d1%": _fmt(m["d1"]),
        "d3%": _fmt(m["d3"]),
        "d7%": _fmt(m["d7"]),
        "ma5": _fmt(m["ma5"], 1),
        "ma20": _fmt(m["ma20"], 1),
        "連続日数": m["streak"],
        "変動%": _fmt(m["vol20"]),
        "有効観測日": m["valid_days"],
        "記録日数": len(marked),
        "判定根拠": basis,
        "直近1週間": price_trail(marked),
        "card_id": card.get("card_id", ""),
        "更新日時": timestamp_jst(),
    }


def build_status_rows(card_rows: list[dict], only_ids: set | None = None) -> list[dict]:
    """履歴から現況ボードの行を組み立てる。

    only_ids を渡すと、その card_id だけに絞る (品番を入れた銘柄だけを載せる)。
    """
    by_key: dict[tuple, list[dict]] = {}
    info: dict[str, dict] = {}
    for r in card_rows or []:
        cid = r.get("card_id", "")
        if not cid or (only_ids is not None and cid not in only_ids):
            continue
        by_key.setdefault((cid, r.get("condition", "")), []).append(r)
        # 名前・品番は後の行ほど新しいので上書きしていく
        if r.get("card_name") or r.get("hinban"):
            info[cid] = {
                "card_id": cid,
                "card_name": r.get("card_name") or info.get(cid, {}).get("card_name", ""),
                "hinban": r.get("hinban") or info.get(cid, {}).get("hinban", ""),
            }

    rows = []
    for (cid, cond), rs in by_key.items():
        series = _to_series(rs)
        if not series:
            continue
        rows.append(build_status_row(info.get(cid, {"card_id": cid}), cond, series))

    # 下落を上に、次に判定不可 (注意が要る順)。シート上でも目に付きやすくする。
    order = {"下落": 0, "判定不可": 1, "横ばい": 2, "上昇": 3}
    rows.sort(key=lambda r: (order.get(r["傾向"], 9), r.get("品番") or "", r.get("状態") or ""))
    return rows


def annotate_watchlist_psa10(watch: list[dict], card_rows: list[dict]) -> list[dict]:
    """ウォッチリストへPSA10の最新傾向を付ける。手入力列はそのまま保つ。"""
    wanted = {r.get("card_id") for r in watch if r.get("card_id")}
    status_rows = build_status_rows(card_rows, only_ids=wanted or set())
    trends = {
        r.get("card_id"): r.get("傾向", "判定不可")
        for r in status_rows
        if str(r.get("状態", "")).lower() == "psa10"
    }
    labels = {
        "下落": "▼ PSA10下落：注意",
        "横ばい": "→ PSA10横ばい",
        "上昇": "▲ PSA10上昇",
        "判定不可": "? PSA10判定不可",
    }
    out = []
    for original in watch:
        row = dict(original)
        cid = row.get("card_id", "")
        row["PSA10判定"] = labels.get(trends.get(cid, "判定不可"), "? PSA10判定不可")
        out.append(row)
    return out


def update_watchlist_psa10(sh, card_rows: list[dict], dry_run: bool) -> int:
    watch = load_watchlist(sh)
    if not watch:
        return 0
    annotated = annotate_watchlist_psa10(watch, card_rows)
    upsert_to_sheet(sh, WATCHLIST_WS, WATCHLIST_HEADERS, annotated,
                    WATCHLIST_KEY_FIELDS, dry_run)
    log(f"  {WATCHLIST_WS}: PSA10判定を {len(annotated)}件更新")
    return len(annotated)


# ===========================================================================
# upsert (追記型。ws.clear() は使わない)
# ===========================================================================
def record_key(record: dict, key_fields: list[str]) -> tuple:
    """upsert のキーを作る。値は文字列化して比較のゆれを消す。"""
    return tuple(str(record.get(f, "") or "").strip() for f in key_fields)


def plan_upsert(existing_grid, records, headers, key_fields):
    """シートの現状と投入したいレコードから、追加行と更新行を計画する。

    純粋関数。API を叩かないのでテストできる。
      existing_grid : シートの全行 (先頭がヘッダー。空リスト可)
      records       : 投入したい dict のリスト
      headers       : このシートの必須列
      key_fields    : 一意性を決める列
    返り値: (header, appends, updates, stats)
      appends : 末尾に足す行のリスト
      updates : (シート上の1始まり行番号, 行データ) のリスト
    ★同じデータを2回投入しても appends は空になり、行は重複しない。
    """
    grid = [list(r) for r in (existing_grid or [])]
    header = list(grid[0]) if grid else []
    if not header:
        header = list(headers)
    else:
        # 人間が足した列は保持したまま、足りない必須列だけ末尾に追加する
        for h in headers:
            if h not in header:
                header.append(h)

    width = len(header)
    idx = {h: header.index(h) for h in header}

    def row_from(record: dict, base: list | None = None) -> list:
        row = list(base) if base else [""] * width
        row += [""] * (width - len(row))
        row = row[:width]
        for h in headers:
            if h in record:
                v = record[h]
                row[idx[h]] = "" if v is None else str(v)
        return row

    # 既存行のキー -> 行番号 (シート上は1始まり、ヘッダーが1行目)
    seen: dict[tuple, int] = {}
    for i, raw in enumerate(grid[1:], start=2):
        cells = list(raw) + [""] * (width - len(raw))
        rec = {h: cells[idx[h]] for h in header if idx[h] < len(cells)}
        key = record_key(rec, key_fields)
        if any(key):
            seen.setdefault(key, i)

    appends, updates = [], []
    added_keys: dict[tuple, int] = {}
    for record in records or []:
        key = record_key(record, key_fields)
        if not any(key):
            continue

        if key in seen:
            rownum = seen[key]
            base = grid[rownum - 1] if rownum - 1 < len(grid) else []
            new_row = row_from(record, base)
            old_row = list(base) + [""] * (width - len(base))
            if new_row != old_row[:width]:
                updates.append((rownum, new_row))
                grid[rownum - 1] = new_row
        elif key in added_keys:
            # 同じ実行内で同じキーが2回来た場合は、後のもので上書きする
            appends[added_keys[key]] = row_from(record, appends[added_keys[key]])
        else:
            added_keys[key] = len(appends)
            appends.append(row_from(record))

    stats = {"appended": len(appends), "updated": len(updates), "header": header}
    return header, appends, updates, stats


# ===========================================================================
# Playwright 共通
# ===========================================================================
def _new_page(browser):
    page = browser.new_page(
        user_agent=USER_AGENT,
        locale="ja-JP",
        viewport={"width": 1366, "height": 900},
    )
    # API応答は content-type が text/html かつ暗号化されている。サイト自身が
    # 復号した直後の JSON.parse 入力を捕捉すれば、暗号方式を複製せずに
    # ブラウザ上で表示された事実だけを取得できる。
    page.add_init_script("""
        (() => {
          window.__pkcParsedPayloads = [];
          const original = JSON.parse;
          JSON.parse = function(text, reviver) {
            const value = original.call(this, text, reviver);
            try {
              if (typeof text === 'string' && text.length > 20) {
                window.__pkcParsedPayloads.push(value);
                if (window.__pkcParsedPayloads.length > 2000) {
                  window.__pkcParsedPayloads.shift();
                }
              }
            } catch (_) {}
            return value;
          };
        })();
    """)
    return page


class NetworkRecorder:
    """XHR/fetch のレスポンスを記録する。

    ★probe の最重要機能。JSON を返しているエンドポイントが見つかれば、
    ページを開くより軽く・速く・安定して取れる (backfill では決定的)。
    """

    def __init__(self):
        self.entries: list[dict] = []
        self.json_bodies: list[tuple[str, object]] = []

    def attach(self, page):
        page.on("response", self._on_response)

    def _on_response(self, response):
        try:
            url = response.url
            status = response.status
            ctype = (response.header_value("content-type") or "").lower()
        except Exception:
            return

        entry = {"url": url, "status": status, "content_type": ctype}
        if "json" in ctype:
            try:
                body = response.json()
                entry["json"] = True
                entry["summary"] = _summarize_json(body)
                self.json_bodies.append((url, body))
            except Exception as exc:
                entry["json"] = False
                entry["error"] = str(exc)[:200]
        self.entries.append(entry)


def _summarize_json(obj, depth: int = 0) -> str:
    """JSONの形をひと目で分かる文字列にする (中身は出さない)。"""
    if depth > 2:
        return "..."
    if isinstance(obj, dict):
        keys = list(obj.keys())[:12]
        return "{" + ", ".join(str(k) for k in keys) + ("...}" if len(obj) > 12 else "}")
    if isinstance(obj, list):
        head = _summarize_json(obj[0], depth + 1) if obj else ""
        return f"[{len(obj)}件 {head}]"
    return type(obj).__name__


def goto_with_backoff(page, url: str, max_retries: int = 4):
    """429 / 503 を受けたら指数バックオフで待つ。連続失敗時は中断する。

    ★相手先は個人運営の無料ファンサイト。畳み掛けない。
    """
    delay = REQUEST_DELAY
    last_status = None
    for attempt in range(1, max_retries + 1):
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = resp.status if resp else 0
        last_status = status
        if status not in (429, 503):
            return resp
        wait = delay * (2 ** (attempt - 1))
        log(f"  ! HTTP {status} を受けました。{wait:.0f}秒待って再試行します "
            f"({attempt}/{max_retries})")
        time.sleep(wait)
    raise RuntimeError(f"HTTP {last_status} が続いたため中断しました: {url}")


def _wait_any(page, selectors, timeout_ms: int = 20000) -> str:
    from playwright.sync_api import TimeoutError as PWTimeout

    per = max(2000, timeout_ms // max(1, len(selectors)))
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=per, state="attached")
            return sel
        except (PWTimeout, Exception):
            continue
    return ""


def collect_series(page, recorder: NetworkRecorder) -> list[tuple[str, list[dict]]]:
    """ページから時系列候補をすべて集める。

    APIレスポンス (recorder) と埋め込みJSON (HTML) の両方を対象にする。
    """
    found: list[tuple[str, list[dict]]] = []
    for url, body in recorder.json_bodies:
        for path, series in walk_series(body):
            found.append((f"API {url} :: {path}", series))
    try:
        html = page.content()
    except Exception:
        html = ""
    for blob in find_json_blobs(html):
        for path, series in walk_series(blob):
            found.append((f"埋め込みJSON :: {path}", series))
    try:
        parsed = page.evaluate("window.__pkcParsedPayloads || []")
    except Exception:
        parsed = []
    for i, body in enumerate(parsed):
        for path, series in walk_series(body):
            found.append((f"ブラウザ復号JSON[{i}] :: {path}", series))
        # 実APIの chart-data は1日1行に price_01/02/03 と volume を持つ。
        # それぞれ 美品/キズあり/PSA10 の独立系列へ展開する。
        for path, rows in walk_chart_rows(body):
            for status, key in (("美品", "price_01"), ("キズあり", "price_02"), ("PSA10", "price_03")):
                series = []
                for row in rows:
                    price = parse_price(row.get(key))
                    date = parse_date(row.get("date"))
                    if date and price is not None:
                        series.append({"date": date, "price": price,
                                       "trade_count": parse_price(row.get("volume"))})
                if series:
                    found.append((f"ブラウザ復号chart-data[{i}] :: {path} :: {status}", series))
    # 長い系列ほど本命 (backfill に使える) なので先に並べる
    found.sort(key=lambda x: len(x[1]), reverse=True)
    return found


def walk_chart_rows(obj, path: str = "", depth: int = 0):
    """復号済み chart-data 配列を再帰的に探す。"""
    if depth > 12:
        return []
    found = []
    if isinstance(obj, list):
        if obj and all(isinstance(v, dict) for v in obj):
            if any("date" in v and any(k in v for k in ("price_01", "price_02", "price_03")) for v in obj):
                found.append((path, obj))
        for i, value in enumerate(obj):
            found.extend(walk_chart_rows(value, f"{path}[{i}]", depth + 1))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(walk_chart_rows(value, f"{path}.{key}" if path else str(key), depth + 1))
    return found


# ===========================================================================
# probe モード (セレクタ/エンドポイント特定用)
# ===========================================================================
def run_probe(url: str) -> int:
    from playwright.sync_api import sync_playwright

    if not url:
        url = URLS["index"]
        log(f"PKC_PROBE_URL が未設定のため既定URLを使います: {url}")

    if not assert_robots_allowed([url]):
        return 1

    log(f"=== probe モード: {url} ===")
    log(f"  User-Agent: {USER_AGENT}")
    if not CONTACT:
        log("  ! PKC_CONTACT が未設定です。相手先は個人運営サイトなので、")
        log("    連絡先を User-Agent に入れることを強く推奨します。")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = _new_page(browser)
        recorder = NetworkRecorder()
        recorder.attach(page)
        try:
            goto_with_backoff(page, url)
            hit = _wait_any(page, READY_SELECTORS)
            log(f"  描画待ちセレクタ: {hit or '(全滅)'}")
            time.sleep(REQUEST_DELAY)
            # 遅延読み込みのAPIを発火させるため、少しスクロールする
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            time.sleep(REQUEST_DELAY)

            html = page.content()
            text = page.inner_text("body")
            with open("pkc_probe_page.html", "w", encoding="utf-8") as f:
                f.write(html)
            with open("pkc_probe_text.txt", "w", encoding="utf-8") as f:
                f.write(text)
            page.screenshot(path="pkc_probe_screenshot.png", full_page=True)
            with open("pkc_probe_network.json", "w", encoding="utf-8") as f:
                json.dump(recorder.entries, f, ensure_ascii=False, indent=1)
            parsed_payloads = page.evaluate("window.__pkcParsedPayloads || []")
            with open("pkc_probe_parsed.json", "w", encoding="utf-8") as f:
                json.dump(parsed_payloads, f, ensure_ascii=False, indent=1, default=str)
            log(f"  ブラウザ内JSON.parse捕捉: {len(parsed_payloads)}件")
            log("  pkc_probe_page.html / pkc_probe_text.txt / pkc_probe_screenshot.png "
                "/ pkc_probe_network.json を保存しました。")

            # --- ★最重要: JSONを返しているエンドポイントの一覧 ---
            log("--- JSONを返したエンドポイント (backfill 可否の判断材料) ---")
            js = [e for e in recorder.entries if e.get("json")]
            if js:
                for e in js:
                    log(f"  [{e['status']}] {e['url']}")
                    log(f"        形: {e.get('summary', '')}")
                log("  ※ backfill に使えそうなものを API_ENDPOINTS に追記してください。")
            else:
                log("  x JSONレスポンスは記録されませんでした。")
                log("    ページ遷移で発火する可能性があるので、カード個別ページのURLでも")
                log("    probe を実行してみてください。")

            # --- 時系列候補 ---
            log("--- 時系列として解釈できた配列 ---")
            found = collect_series(page, recorder)
            if not found:
                log("  x 時系列を抽出できませんでした。DATE_KEYS / PRICE_KEYS の候補が")
                log("    実際のキー名と合っていない可能性があります。")
                log("    pkc_probe_network.json と pkc_probe_page.html を確認してください。")
            for src, series in found[:10]:
                span = f"{series[0]['date']} 〜 {series[-1]['date']}" if series else ""
                log(f"  o {len(series)}点 ({span})  {src}")
                log(f"      先頭: {series[0]}")

            # --- ★成否を分ける1点: trade_count が取れるか ---
            log("--- ★取引件数 (trade_count) の取得可否 ---")
            with_count = [(s, ser) for s, ser in found if has_trade_count(ser)]
            if with_count:
                log(f"  o 取得できます。取引件数を含む系列が {len(with_count)} 本ありました。")
                for s, ser in with_count[:3]:
                    log(f"    {s}")
                    log(f"      例: {ser[-1]}")
                log("  → 補完日を除外した実測ベースで傾向判定できます。")
            else:
                log("  x 取引件数を含む系列は見つかりませんでした。")
                log("    このままだと補完日 (取引が無く前日価格を引き継いだ日) を")
                log("    実測と区別できず、下落の立ち上がりを検知できません。")
                log("    COUNT_KEYS の候補を実キー名に合わせるか、")
                log("    フォールバック (前日と価格が完全一致した日を除外) で運用します。")

            # --- 鑑定特化モード ---
            log("--- 鑑定特化モード (PSA10) の切り替え要素 ---")
            for sel in APPRAISAL_TOGGLE_SELECTORS:
                try:
                    n = len(page.query_selector_all(sel))
                except Exception:
                    n = -1
                log(f"  {'o' if n > 0 else 'x'} {sel}  ヒット{n}件")

            log("--- DOMフォールバック用セレクタのヒット状況 ---")
            for name, sels in (
                ("指数の値", INDEX_VALUE_SELECTORS),
                ("カードリンク", CARD_LINK_SELECTORS),
                ("カード行", CARD_ROW_SELECTORS),
            ):
                log(f"[{name}]")
                for sel in sels:
                    try:
                        n = len(page.query_selector_all(sel))
                    except Exception:
                        n = -1
                    log(f"  {'o' if n > 0 else 'x'} {sel}  ヒット{n}件")
            return 0
        except Exception as exc:
            import traceback

            log(f"! probe 中に例外: {exc}")
            log(traceback.format_exc())
            try:
                with open("pkc_probe_network.json", "w", encoding="utf-8") as f:
                    json.dump(recorder.entries, f, ensure_ascii=False, indent=1)
                page.screenshot(path="pkc_probe_screenshot.png", full_page=True)
            except Exception:
                pass
            return 1
        finally:
            browser.close()


# ===========================================================================
# 取得 (index / cards / master / backfill)
# ===========================================================================
def _open_and_collect(page, recorder, url: str):
    """1ページ開いて時系列候補を集める共通処理。"""
    # 同じpageを複数カードに再利用するため、直前カードの復号データを必ず捨てる。
    try:
        page.evaluate("window.__pkcParsedPayloads = []")
    except Exception:
        pass
    recorder.json_bodies.clear()
    goto_with_backoff(page, url)
    _wait_any(page, READY_SELECTORS)
    time.sleep(REQUEST_DELAY)
    return collect_series(page, recorder)


def fetch_index(page, recorder, appraisal: bool = False) -> list[dict]:
    """指数の当日値を取得する。

    JSON から取れた系列の最終点を当日値として採用する。
    index_type は通常モード/鑑定モードで分ける。
    """
    url = URLS["index"]
    if appraisal and APPRAISAL_QUERY_CANDIDATES:
        url = url.rstrip("/") + "/" + APPRAISAL_QUERY_CANDIDATES[0]
    found = _open_and_collect(page, recorder, url)
    if not found:
        log("  ! 指数の時系列を抽出できませんでした。probe で設定を確認してください。")
        return []

    src, series = found[0]
    label = "psa10" if appraisal else "bihin"
    log(f"  指数系列 {len(series)}点 を採用 ({src})")

    records = []
    prev = None
    for r in series[-30:]:  # 運用に必要なのは直近20〜30日
        diff = diff_pct = ""
        if prev is not None:
            diff = str(r["price"] - prev)
            pc = _pct_change(r["price"], prev)
            diff_pct = _fmt(pc)
        records.append({
            "date": r["date"],
            "index_type": label,
            "value": r["price"],
            "diff": diff,
            "diff_pct": diff_pct,
            "fetched_at": timestamp_jst(),
        })
        prev = r["price"]
    return records


def fetch_master(page, recorder) -> list[dict]:
    """/all-card/ からカード一覧を取得する。"""
    url = URLS["all_card"]
    goto_with_backoff(page, url)
    _wait_any(page, READY_SELECTORS)
    time.sleep(REQUEST_DELAY)

    seen, records = set(), []
    for sel in CARD_LINK_SELECTORS:
        try:
            els = page.query_selector_all(sel)
        except Exception:
            continue
        for el in els:
            href = el.get_attribute("href") or ""
            cid = slug_from_url(href)
            if not cid or cid in seen:
                continue
            seen.add(cid)
            name = (el.inner_text() or "").strip().replace("\n", " ")
            records.append({
                "card_id": cid,
                "card_name": name,
                "hinban": extract_hinban(name),
                "url": urljoin(BASE, href),
                "last_seen_at": timestamp_jst(),
            })
        if records:
            log(f"  カード一覧 {len(records)}件 (セレクタ: {sel})")
            break

    # 現行サイトは一覧APIを暗号化して返す。ブラウザ自身が復号したitem情報を使う。
    if not records:
        try:
            payloads = page.evaluate("window.__pkcParsedPayloads || []")
        except Exception:
            payloads = []
        for item in walk_item_records(payloads):
            cid = str(item.get("strSlug") or "").strip()
            name = str(item.get("strName") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            records.append({
                "card_id": cid,
                "card_name": name,
                "hinban": extract_hinban(name),
                "url": f"{BASE}/{cid}/",
                "last_seen_at": timestamp_jst(),
            })
        if records:
            log(f"  カード一覧 {len(records)}件 (ブラウザ復号item API)")
        else:
            log("  ! カード一覧を抽出できませんでした。probe 結果を確認してください。")
    return records


def walk_item_records(obj, depth: int = 0) -> list[dict]:
    """復号済みitem APIからカードマスタ行を抽出する。"""
    if depth > 10:
        return []
    found = []
    if isinstance(obj, dict):
        if obj.get("strSlug") and obj.get("strName"):
            found.append(obj)
        else:
            for value in obj.values():
                found.extend(walk_item_records(value, depth + 1))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(walk_item_records(value, depth + 1))
    return found


def extract_hinban(text: str) -> str:
    """カード名から品番らしき部分を取り出す (例: "リザードン 006/165" -> "006/165")。"""
    t = unicodedata.normalize("NFKC", text or "")
    m = re.search(r"\b(\d{1,3}\s*/\s*\d{1,3}[A-Za-z\-]*)\b", t)
    return m.group(1).replace(" ", "") if m else ""


def fetch_card(page, recorder, card: dict, days: int = 30) -> list[dict]:
    """カード個別ページから価格の時系列を取得する。

    condition (美品 / キズあり / PSA10) ごとに別系列として記録する。
    どの系列がどの condition かはキー名から推定する (要設定)。
    """
    found = _open_and_collect(page, recorder, card["url"])
    if not found:
        return []

    records = []
    for src, series in found[:3]:  # 上位3系列 = condition 別と想定
        cond = guess_condition(src)
        for r in series[-days:]:
            records.append({
                "date": r["date"],
                "card_id": card["card_id"],
                "card_name": card.get("card_name", ""),
                "hinban": card.get("hinban", ""),
                "condition": cond,
                "price": r["price"],
                "trade_count": "" if r.get("trade_count") is None else r["trade_count"],
                # 取引件数が無い/0 の日は実測ではない。下流に必ず伝える。
                "imputed_suspect": "1" if (r.get("trade_count") is None or r["trade_count"] <= 0) else "",
                "fetched_at": timestamp_jst(),
            })
    return records


# 【要設定】系列のパス名から condition を推定する対応表。
# 実際のキー名は probe で確認して調整すること。
CONDITION_HINTS = [
    ("psa10", ["psa10", "psa_10", "graded", "鑑定"]),
    ("キズあり", ["damaged", "kizu", "played", "キズ"]),
    ("美品", ["mint", "bihin", "clean", "美品", "normal"]),
]


def guess_condition(source_path: str) -> str:
    """系列のパス名から美品/キズあり/PSA10 を推定する。判別不能なら "不明"。"""
    s = (source_path or "").lower()
    for cond, hints in CONDITION_HINTS:
        if any(h.lower() in s for h in hints):
            return cond
    return "不明"


# ===========================================================================
# Google Sheets (★追記型。ws.clear() は使わない)
# ===========================================================================
def _load_service_account_info() -> dict:
    """GOOGLE_SERVICE_ACCOUNT_JSON を辞書化する。

    生のJSON / base64エンコードしたJSON の両方に対応する
    (既存 scrape.py と同じ作法。import はせず独立実装)。
    """
    import base64

    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        raise SystemExit(
            "ERROR: GOOGLE_SERVICE_ACCOUNT_JSON が空です。Secrets に鍵JSONを登録してください。"
        )

    if not raw.startswith("{"):
        try:
            decoded = base64.b64decode(raw, validate=True).decode("utf-8").strip()
            if decoded.startswith("{"):
                raw = decoded
        except Exception:
            pass

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(
            "ERROR: GOOGLE_SERVICE_ACCOUNT_JSON を JSON として解釈できませんでした。\n"
            f"  受け取った値: 長さ={len(raw)}文字 / 先頭文字={raw[:1]!r}\n"
            "  対処: サービスアカウント鍵JSONの中身全体('{' から '}' まで)を\n"
            "        そのまま Secret に貼り付けてください。\n"
            "  (改行で問題が出る場合は base64 エンコードした文字列でも可)"
        )


def _open_spreadsheet():
    import gspread
    from google.oauth2.service_account import Credentials

    info = _load_service_account_info()
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ.get("SPREADSHEET_ID") or DEFAULT_SPREADSHEET_ID)


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _get_or_create(sh, name: str, headers: list[str]):
    import gspread

    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=max(len(headers), 10))
        ws.update([headers], "A1", value_input_option="USER_ENTERED")
        log(f"  シート '{name}' を新規作成しました。")
        return ws


def upsert_to_sheet(sh, name: str, headers: list[str], records: list[dict],
                    key_fields: list[str], dry_run: bool) -> None:
    """追記型でシートへ書き込む。

    ★ws.clear() は絶対に呼ばない。時系列が消えると傾向判定ができなくなる。
    ★1行ずつ API を叩かない。append は append_rows で一括、
      更新は batch_update でまとめて送る。
    """
    if not records:
        log(f"  {name}: 書き込む行がありません。")
        return

    if dry_run:
        log(f"  DRY_RUN: {name} へ {len(records)}件 (サンプル: {records[0]})")
        return

    ws = _get_or_create(sh, name, headers)
    existing = ws.get_all_values()
    header, appends, updates, stats = plan_upsert(existing, records, headers, key_fields)

    # ヘッダーに列を足した場合は先に書き戻す
    if not existing or existing[0] != header:
        ws.update([header], f"A1:{_col_letter(len(header))}1", value_input_option="USER_ENTERED")

    if updates:
        body = [
            {"range": f"A{rownum}:{_col_letter(len(header))}{rownum}", "values": [row]}
            for rownum, row in updates
        ]
        # まとめて送る (1行ずつ API を叩かない)
        for i in range(0, len(body), 100):
            ws.batch_update(body[i:i + 100], value_input_option="USER_ENTERED")

    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED",
                       insert_data_option="INSERT_ROWS", table_range="A1")

    log(f"  {name}: 追加 {stats['appended']}行 / 更新 {stats['updated']}行 "
        f"(既存 {max(0, len(existing) - 1)}行)")


def read_history(sh, name: str, headers: list[str]) -> list[dict]:
    """履歴シートを読み込んで dict のリストにする (trend 計算用)。"""
    import gspread

    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return []
    values = ws.get_all_values()
    if len(values) < 2:
        return []
    header = values[0]
    out = []
    for raw in values[1:]:
        cells = list(raw) + [""] * (len(header) - len(raw))
        out.append({h: cells[i] for i, h in enumerate(header)})
    return out


# ===========================================================================
# trend モード: 蓄積済みデータから傾向を再計算する
# ===========================================================================
def _to_series(rows: list[dict], price_key: str = "price") -> list[dict]:
    """シートの行を計算用の系列に変換する。"""
    series = []
    for r in rows:
        price = parse_price(r.get(price_key))
        if price is None or not r.get("date"):
            continue
        tc_raw = r.get("trade_count", "")
        tc = parse_price(tc_raw) if str(tc_raw).strip() != "" else None
        series.append({"date": r["date"], "price": price, "trade_count": tc})
    return series


def compute_all_trends(index_rows: list[dict], card_rows: list[dict]) -> list[dict]:
    """指数 / カード個別 / グループ集約の傾向をまとめて計算する。

    ★個別カードは取引が薄く1〜2件で跳ねるので、
      グループ集約の傾向を必ず併せて出す (依頼の要件)。
    """
    out = []

    # --- 指数 ---
    by_type: dict[str, list[dict]] = {}
    for r in index_rows:
        t = r.get("index_type") or "unknown"
        by_type.setdefault(t, []).append(r)
    for t, rows in by_type.items():
        series = _to_series(rows, price_key="value")
        # 指数には取引件数が無いので price_repeat フォールバックになる
        out.append(build_trend_row("index", t, series))

    # --- カード個別 (card_id + condition 単位) ---
    by_card: dict[tuple, list[dict]] = {}
    for r in card_rows:
        key = (r.get("card_id", ""), r.get("condition", ""))
        if not key[0]:
            continue
        by_card.setdefault(key, []).append(r)
    for (cid, cond), rows in by_card.items():
        series = _to_series(rows)
        name = next((r.get("card_name") for r in reversed(rows) if r.get("card_name")), "")
        hinban = next((r.get("hinban") for r in reversed(rows) if r.get("hinban")), "")
        label = " ".join(x for x in [name, hinban and f"[{hinban}]", cond] if x)
        out.append(build_trend_row("card", f"{cid}|{cond}", series, label=label))

    # --- グループ集約 (condition 単位。個別より安定する) ---
    series_by_group = group_series(
        [
            {
                "date": r.get("date", ""),
                "price": parse_price(r.get("price")),
                "trade_count": (
                    parse_price(r.get("trade_count"))
                    if str(r.get("trade_count", "")).strip() != "" else None
                ),
                "condition": r.get("condition", ""),
                "hinban": r.get("hinban", ""),
            }
            for r in card_rows
        ],
        group_of=lambda r: r.get("condition") or "",
    )
    for g, series in series_by_group.items():
        out.append(build_trend_row("group", f"condition:{g}", series))

    # --- グループ集約 (品番の系統。新弾/旧弾の代理指標) ---
    series_by_set = group_series(
        [
            {
                "date": r.get("date", ""),
                "price": parse_price(r.get("price")),
                "trade_count": (
                    parse_price(r.get("trade_count"))
                    if str(r.get("trade_count", "")).strip() != "" else None
                ),
                "hinban": r.get("hinban", ""),
            }
            for r in card_rows
        ],
        group_of=lambda r: (r.get("hinban") or "").split("/")[-1] if r.get("hinban") else "",
    )
    for g, series in series_by_set.items():
        out.append(build_trend_row("group", f"set:{g}", series))

    return out


def log_trend_summary(rows: list[dict]) -> None:
    """傾向の内訳をログに出す (人間が信頼度を見られるように)。"""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["trend"]] = counts.get(r["trend"], 0) + 1
    log(f"  傾向の内訳: {counts}")

    downs = [r for r in rows if r["trend"] == "下落"]
    if downs:
        log("  ▼ 下落判定 (買取率を下げる検討対象):")
        for r in sorted(downs, key=lambda x: float(x["d3"] or 0))[:15]:
            log(f"    {r['scope']:5} {r['key'][:40]:40} d3={r['d3']}% "
                f"streak={r['streak']} 有効{r['valid_days']}日 "
                f"バッファ目安={r['suggested_buffer_pct'] or '-'}%")

    undecidable = [r for r in rows if r["trend"] == "判定不可"]
    if undecidable:
        log(f"  判定不可 {len(undecidable)}件 (有効観測日 {MIN_VALID_DAYS}日未満)。"
            f"薄い銘柄なので無理に判定していません。")

    basis = {r["imputation_basis"] for r in rows}
    if "price_repeat" in basis:
        log("  ! 一部の系列で取引件数が取れていないため、")
        log("    「前日と価格が完全一致した日」を補完候補として除外しています。")
        log("    実際に値動きが無かっただけの日も巻き込むため、精度は落ちます。")


# ===========================================================================
# 1〜3か月市場分析
# ===========================================================================
UNAVAILABLE = "取得不能"


def _number(value):
    """シート値を float にする。取得不能・空欄は None。"""
    if value in (None, "", UNAVAILABLE):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _pct(current, previous):
    current, previous = _number(current), _number(previous)
    if current is None or previous in (None, 0):
        return None
    return (current / previous - 1) * 100


def _display(value, digits=2):
    if value is None or value == "":
        return UNAVAILABLE
    if isinstance(value, float):
        return round(value, digits)
    return value


def _date_le(date_text: str, as_of: str) -> bool:
    return bool(date_text and as_of and date_text <= as_of)


def _on_or_before(series: list[dict], target: str):
    rows = [r for r in series if _date_le(r.get("date", ""), target)]
    return rows[-1] if rows else None


def _days_before(as_of: str, days: int) -> str:
    return (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


def _median(values):
    nums = [_number(v) for v in values]
    nums = [v for v in nums if v is not None]
    return statistics.median(nums) if nums else None


def _probabilities(score, confidence_factor=1.0, horizon=1):
    """スコアを確率へ写像する。断定を避け、低取得率ほど横ばいへ寄せる。"""
    if score is None:
        return (25, 50, 25)
    tilt = max(-25.0, min(25.0, (score - 50) * (0.75 if horizon == 1 else 0.9)))
    tilt *= confidence_factor
    up = 25 + max(tilt, 0)
    down = 25 + max(-tilt, 0)
    flat = 100 - up - down
    return tuple(int(round(v)) for v in (up, flat, down))


def _weighted_score(components: dict) -> tuple[float | None, float]:
    """取得できた配点だけを100点換算。戻り値は score, coverage。"""
    available = {k: v for k, v in components.items() if v[1] is not None}
    total_weight = sum(weight for weight, _ in available.values())
    if not total_weight:
        return None, 0.0
    earned = sum(weight * max(0, min(1, raw)) for weight, raw in available.values())
    return earned / total_weight * 100, total_weight


def _component_scores(d30, relative, sales30, listings, supply30, premium):
    # 各 raw は 0〜1。中立を0.5とし、極端な値はクリップする。
    trend = None if d30 is None else 0.5 + max(-25, min(25, d30)) / 50
    rel = None if relative is None else 0.5 + max(-20, min(20, relative)) / 40
    if sales30 is None or listings is None:
        liquidity = None
    elif listings == 0:
        liquidity = 1.0 if sales30 > 0 else 0.5
    else:
        liquidity = min(1.0, sales30 / listings)
    # 供給増が大きいほど低得点。増加率が0なら満点ではなく0.75。
    supply = None if supply30 is None else max(0.0, min(1.0, 0.75 - supply30 / 20))
    # PSA10倍率1.5〜3倍を中立域、過度なプレミアムは減点。
    balance = None if premium is None else max(0.0, min(1.0, 1 - abs(premium - 2.25) / 4.5))
    return {
        "価格トレンド点": (25, trend), "相対強度点": (20, rel),
        "流動性点": (20, liquidity), "供給リスク点": (25, supply),
        "価格バランス点": (10, balance),
    }


def _phase(score, d30, drawdown):
    if score is None:
        return "判断不能"
    if d30 is not None and d30 > 10 and drawdown is not None and drawdown > -5:
        return "上昇後期"
    if score >= 65:
        return "上昇初期"
    if drawdown is not None and drawdown < -20 and d30 is not None and d30 >= 0:
        return "底固め"
    if score < 45:
        return "調整中"
    return "判断不能"


def build_market_analysis(request: dict, card_rows: list[dict], index_rows: list[dict],
                          supply_rows: list[dict], liquidity_rows: list[dict]):
    """1カードの要約行と縦持ち指標行を作る。外部値が無ければ取得不能。"""
    cid = (request.get("card_id") or "").strip()
    as_of = parse_date(request.get("分析基準日")) or today_jst()
    own = [r for r in card_rows if r.get("card_id") == cid and _date_le(r.get("date", ""), as_of)]
    psa = sorted(_to_series([r for r in own if str(r.get("condition", "")).lower() == "psa10"]),
                 key=lambda r: r["date"])
    raw = sorted(_to_series([r for r in own if r.get("condition") == "美品"]), key=lambda r: r["date"])
    idx = sorted(_to_series([dict(r, price=r.get("value")) for r in index_rows
                             if str(r.get("index_type", "")).lower() == "psa10"
                             and _date_le(r.get("date", ""), as_of)]), key=lambda r: r["date"])

    current_row = _on_or_before(psa, as_of)
    current = current_row.get("price") if current_row else None
    points = {d: _on_or_before(psa, _days_before(as_of, d)) for d in (7, 30, 90, 180)}
    prices = {d: (points[d].get("price") if points[d] else None) for d in points}
    changes = {d: _pct(current, prices[d]) for d in (7, 30, 90)}
    since90 = [r for r in psa if r["date"] >= _days_before(as_of, 90)]
    high_row = max(since90, key=lambda r: r["price"]) if since90 else None
    low_row = min(since90, key=lambda r: r["price"]) if since90 else None
    drawdown = _pct(current, high_row.get("price") if high_row else None)
    raw_row = _on_or_before(raw, as_of)
    raw_price = raw_row.get("price") if raw_row else None
    premium = (current / raw_price) if current is not None and raw_price not in (None, 0) else None

    index_now_row = _on_or_before(idx, as_of)
    index_now = index_now_row.get("price") if index_now_row else None
    index_changes = {}
    for d in (7, 30, 90):
        old = _on_or_before(idx, _days_before(as_of, d))
        index_changes[d] = _pct(index_now, old.get("price") if old else None)
    relative = None if changes[30] is None or index_changes[30] is None else changes[30] - index_changes[30]

    supply = sorted([r for r in supply_rows if r.get("card_id") == cid and _date_le(r.get("date", ""), as_of)],
                    key=lambda r: r.get("date", ""))
    supply_now = _on_or_before(supply, as_of)
    s30 = _on_or_before(supply, _days_before(as_of, 30))
    s90 = _on_or_before(supply, _days_before(as_of, 90))
    psa10_count = _number(supply_now.get("psa10_count")) if supply_now else None
    all_count = _number(supply_now.get("all_grade_count")) if supply_now else None
    psa_rate = (psa10_count / all_count * 100) if psa10_count is not None and all_count else None
    inc30 = psa10_count - _number(s30.get("psa10_count")) if psa10_count is not None and s30 and _number(s30.get("psa10_count")) is not None else None
    inc90 = psa10_count - _number(s90.get("psa10_count")) if psa10_count is not None and s90 and _number(s90.get("psa10_count")) is not None else None
    supply30 = _pct(psa10_count, s30.get("psa10_count")) if s30 else None
    supply90 = _pct(psa10_count, s90.get("psa10_count")) if s90 else None

    liq = [r for r in liquidity_rows if r.get("card_id") == cid and _date_le(r.get("date", ""), as_of)]
    sales = sorted([r for r in liq if r.get("record_type") == "sale" and str(r.get("is_duplicate", "")) != "1"],
                   key=lambda r: r.get("date", ""))
    dupes = len([r for r in liq if r.get("record_type") == "sale" and str(r.get("is_duplicate", "")) == "1"])
    sales30_rows = [r for r in sales if r.get("date", "") >= _days_before(as_of, 30)]
    sales90_rows = [r for r in sales if r.get("date", "") >= _days_before(as_of, 90)]
    listing_snapshots = sorted([r for r in liq if r.get("record_type") == "listing_snapshot"], key=lambda r: r.get("date", ""))
    listing_row = _on_or_before(listing_snapshots, as_of)
    listings = _number(listing_row.get("current_listings")) if listing_row else None
    gaps = []
    for a, b in zip(sales90_rows, sales90_rows[1:]):
        gaps.append((datetime.strptime(b["date"], "%Y-%m-%d") - datetime.strptime(a["date"], "%Y-%m-%d")).days)
    sale_prices = [_number(r.get("price")) for r in sales90_rows if _number(r.get("price")) is not None]
    recent_prices = [_number(r.get("price")) for r in sales[-10:] if _number(r.get("price")) is not None]
    absorption = (len(sales30_rows) / listings) if listings not in (None, 0) else None
    months = (listings / len(sales30_rows)) if listings is not None and sales30_rows else None

    components = _component_scores(changes[30], relative, len(sales30_rows) if liq else None,
                                   listings, supply30, premium)
    score, coverage = _weighted_score(components)
    confidence = "高" if coverage >= 80 else "中" if coverage >= 55 else "低"
    confidence_factor = max(0.35, coverage / 100)
    p1 = _probabilities(score, confidence_factor, 1)
    p3 = _probabilities(score, confidence_factor, 3)

    positives, negatives = [], []
    if relative is not None and relative > 3: positives.append(f"市場指数より30日で{relative:.1f}pt強い")
    if relative is not None and relative < -3: negatives.append(f"市場指数より30日で{abs(relative):.1f}pt弱い")
    if supply30 is not None and supply30 > 5: negatives.append(f"PSA10供給が30日で{supply30:.1f}%増加")
    if supply30 is not None and supply30 <= 2 and changes[30] is not None and changes[30] >= 0: positives.append("供給増が限定的で価格を維持")
    if absorption is not None and absorption >= 1: positives.append(f"需給吸収率{absorption:.2f}")
    if months is not None and months >= 2: negatives.append(f"販売在庫{months:.1f}か月")
    if changes[30] is not None and changes[30] > 5: positives.append(f"30日騰落率+{changes[30]:.1f}%")
    if drawdown is not None and drawdown < -15: negatives.append(f"90日高値から{drawdown:.1f}%")
    if premium is not None and premium > 5: negatives.append(f"PSA10倍率{premium:.2f}倍")

    component_points = {k: (None if raw_score is None else round(weight * raw_score, 1))
                        for k, (weight, raw_score) in components.items()}
    base_price = current
    volatility = statistics.pstdev([r["price"] for r in since90]) / statistics.mean([r["price"] for r in since90]) if len(since90) >= 2 and statistics.mean([r["price"] for r in since90]) else 0.1
    band = max(0.08, min(0.3, volatility))
    sources = [request.get("カードURL")]
    sources += [r.get("source_url") for r in (supply_now, listing_row) if r]
    sources = [s for s in dict.fromkeys(sources) if s]

    row = {
        "分析基準日": as_of, "card_id": cid, "カード名": request.get("カード名", ""),
        "カード番号": request.get("カード番号", ""), "収録商品": request.get("収録商品・プロモ名", ""),
        "言語": request.get("言語") or "日本語版", "グレード": request.get("グレード") or "PSA10",
        "現在PSA10相場": _display(current), "7日前": _display(prices[7]), "30日前": _display(prices[30]),
        "90日前": _display(prices[90]), "180日前": _display(prices[180]),
        "90日最高値": _display(high_row.get("price") if high_row else None),
        "最高値日": _display(high_row.get("date") if high_row else None),
        "90日最安値": _display(low_row.get("price") if low_row else None),
        "7日騰落率%": _display(changes[7]), "30日騰落率%": _display(changes[30]),
        "90日騰落率%": _display(changes[90]), "最高値からの下落率%": _display(drawdown),
        "価格更新日": _display(current_row.get("date") if current_row else None),
        "直近取引日": _display(next((r["date"] for r in reversed(psa) if r.get("trade_count") and r["trade_count"] > 0), None)),
        "90日観測数": len(since90), "未鑑定品相場": _display(raw_price),
        "PSA10価格差": _display(current - raw_price if current is not None and raw_price is not None else None),
        "PSA10プレミアム倍率": _display(premium), "PSA10市場指数": _display(index_now),
        "指数7日%": _display(index_changes[7]), "指数30日%": _display(index_changes[30]),
        "指数90日%": _display(index_changes[90]), "市場相対強度": _display(relative),
        "PSA10枚数": _display(psa10_count), "全グレード枚数": _display(all_count), "PSA10率%": _display(psa_rate),
        "PSA10_30日増加数": _display(inc30), "PSA10_90日増加数": _display(inc90),
        "PSA10_30日増加率%": _display(supply30), "PSA10_90日増加率%": _display(supply90),
        "30日成約件数": len(sales30_rows) if liq else UNAVAILABLE, "90日成約件数": len(sales90_rows) if liq else UNAVAILABLE,
        "直近成約日": _display(sales[-1].get("date") if sales else None), "成約間隔中央値日": _display(_median(gaps)),
        "直近成約価格中央値": _display(_median(recent_prices)), "最高成約価格": _display(max(sale_prices) if sale_prices else None),
        "最低成約価格": _display(min(sale_prices) if sale_prices else None), "重複候補数": dupes if liq else UNAVAILABLE,
        "現在出品数": _display(listings), "需給吸収率": _display(absorption),
        "販売在庫月数": "流動性極小" if listings is not None and not sales30_rows else _display(months),
        **{k: _display(v, 1) for k, v in component_points.items()},
        "相場強度スコア": _display(round(score, 1) if score is not None else None),
        "データ取得率%": round(coverage, 1), "予測信頼度": confidence,
        "市場フェーズ": _phase(score, changes[30], drawdown),
        "1か月上昇%": p1[0], "1か月横ばい%": p1[1], "1か月下落%": p1[2],
        "3か月上昇%": p3[0], "3か月横ばい%": p3[1], "3か月下落%": p3[2],
        "強気価格": _display(round(base_price * (1 + band)) if base_price else None),
        "基本価格": _display(round(base_price) if base_price else None),
        "弱気価格": _display(round(base_price * (1 - band)) if base_price else None),
        "上昇要因": " / ".join(positives[:5]) or UNAVAILABLE,
        "下落要因": " / ".join(negatives[:5]) or UNAVAILABLE,
        "最重要先行指標": (negatives or positives or ["外部データの取得"])[0],
        "次回確認条件": "30日後、またはPSA10枚数・出品数・成約中央値の更新時",
        "参照URL": "\n".join(sources) or UNAVAILABLE, "更新日時": timestamp_jst(),
    }
    metric_specs = [
        ("現在PSA10相場", row["現在PSA10相場"], row["30日前"], row["30日騰落率%"], "みんなのポケカ相場"),
        ("PSA10市場指数", row["PSA10市場指数"], UNAVAILABLE, row["指数30日%"], "みんなのポケカ相場"),
        ("PSA10枚数", row["PSA10枚数"], _display(s30.get("psa10_count") if s30 else None), row["PSA10_30日増加率%"], "PSA Population Report"),
        ("30日成約件数", row["30日成約件数"], row["現在出品数"], row["需給吸収率"], "成約・出品履歴"),
        ("PSA10プレミアム倍率", row["PSA10プレミアム倍率"], row["未鑑定品相場"], UNAVAILABLE, "みんなのポケカ相場"),
    ]
    metric_rows = [{
        "分析基準日": as_of, "card_id": cid, "カード名": row["カード名"], "指標": name,
        "現在値": now, "比較値": comp, "変化率": change,
        "判定": UNAVAILABLE if now == UNAVAILABLE else "要監視", "取得元": source,
        "source_url": row["参照URL"], "更新日時": row["更新日時"],
    } for name, now, comp, change, source in metric_specs]
    return row, metric_rows


def build_market_analyses(requests, card_rows, index_rows, supply_rows, liquidity_rows):
    summaries, metrics = [], []
    for request in requests:
        if str(request.get("有効", "1")).strip().lower() in ("0", "false", "off", "無効"):
            continue
        if not request.get("card_id"):
            continue
        summary, detail = build_market_analysis(request, card_rows, index_rows, supply_rows, liquidity_rows)
        summaries.append(summary)
        metrics.extend(detail)
    return summaries, metrics


def load_analysis_requests(sh) -> list[dict]:
    """分析依頼を読む。空ならウォッチリストからPSA10分析依頼を生成する。"""
    requests = read_history(sh, ANALYSIS_REQUEST_WS, ANALYSIS_REQUEST_HEADERS)
    requests = [r for r in requests if r.get("card_id") or r.get("カード番号") or r.get("カード名")]
    if requests:
        return requests
    watch = load_watchlist(sh)
    generated = []
    for w in watch:
        if not w.get("card_id"):
            continue
        generated.append({
            "card_id": w.get("card_id", ""), "カード名": w.get("名前", ""),
            "カード番号": w.get("品番", ""), "収録商品・プロモ名": "",
            "言語": "日本語版", "グレード": "PSA10", "分析基準日": today_jst(),
            "カードURL": w.get("url", ""), "有効": "1", "メモ": "ウォッチリストから自動生成",
        })
    return generated


def run_market_analysis(sh, dry_run: bool) -> int:
    requests = load_analysis_requests(sh)
    if not requests:
        log(f"! {ANALYSIS_REQUEST_WS} に分析対象がありません。card_id を入力してください。")
        if not dry_run:
            _get_or_create(sh, ANALYSIS_REQUEST_WS, ANALYSIS_REQUEST_HEADERS)
            _get_or_create(sh, PSA_SUPPLY_WS, PSA_SUPPLY_HEADERS)
            _get_or_create(sh, LIQUIDITY_WS, LIQUIDITY_HEADERS)
        return 0
    if not dry_run:
        upsert_to_sheet(sh, ANALYSIS_REQUEST_WS, ANALYSIS_REQUEST_HEADERS, requests,
                        ["card_id"], dry_run)
        _get_or_create(sh, PSA_SUPPLY_WS, PSA_SUPPLY_HEADERS)
        _get_or_create(sh, LIQUIDITY_WS, LIQUIDITY_HEADERS)
    card_rows = read_history(sh, CARD_WS, CARD_HEADERS)
    update_watchlist_psa10(sh, card_rows, dry_run)
    index_rows = read_history(sh, INDEX_WS, INDEX_HEADERS)
    supply_rows = read_history(sh, PSA_SUPPLY_WS, PSA_SUPPLY_HEADERS)
    liquidity_rows = read_history(sh, LIQUIDITY_WS, LIQUIDITY_HEADERS)
    summaries, metrics = build_market_analyses(
        requests, card_rows, index_rows, supply_rows, liquidity_rows)
    log(f"  市場分析: {len(summaries)}銘柄 / 指標 {len(metrics)}行")
    upsert_to_sheet(sh, ANALYSIS_WS, ANALYSIS_HEADERS, summaries, ANALYSIS_KEY_FIELDS, dry_run)
    upsert_to_sheet(sh, ANALYSIS_METRIC_WS, ANALYSIS_METRIC_HEADERS, metrics,
                    ANALYSIS_METRIC_KEY_FIELDS, dry_run)
    return len(summaries)


# ===========================================================================
# main
# ===========================================================================
VALID_MODES = ("probe", "backfill", "index", "cards", "master", "trend", "daily",
               "card", "watchlist", "analysis")


def main() -> int:
    dry_run = os.environ.get("DRY_RUN") == "1"

    if MODE not in VALID_MODES:
        log(f"ERROR: PKC_MODE が不正です: {MODE!r} ({' / '.join(VALID_MODES)})")
        return 1

    log("=== みんなのポケカ相場 スクレイパー 開始 ===")
    log(f"  mode={MODE} / dry_run={dry_run} / delay={REQUEST_DELAY}秒 / max_items={MAX_ITEMS or '無制限'}")
    if REQUEST_DELAY <= MIN_REQUEST_DELAY:
        log(f"  ※ リクエスト間隔は {MIN_REQUEST_DELAY}秒 未満に下げられません "
            f"(個人運営の無料ファンサイトへの配慮)。")

    if MODE == "probe":
        return run_probe(PROBE_URL)

    if not dry_run and not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        log("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
        log("  リポジトリの Settings → Secrets and variables → Actions に登録してください。")
        return 1
    if not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        log("ERROR: シートの読み書きに鍵が必要です。DRY_RUN でも設定してください。")
        log("  ネットワークまわりだけ確認したい場合は PKC_MODE=probe を使ってください。")
        return 1

    sh = _open_spreadsheet()

    # --- card は蓄積済みデータだけで動く (サイトへアクセスしない) ---
    # 「この銘柄は今どうなってる?」を調べるためのモード。
    # 何度実行しても相手先に負荷はかからない。
    if MODE == "card":
        if not CARD_QUERY:
            log("ERROR: PKC_CARD_QUERY が未設定です。調べたい銘柄を指定してください。")
            log("  品番 / カード名 / card_id / カードURL のいずれでも引けます。")
            log("  例: PKC_MODE=card PKC_CARD_QUERY='006/165' python scraper/pokeca_scrape.py")
            return 1
        card_rows = read_history(sh, CARD_WS, CARD_HEADERS)
        if not card_rows:
            log(f"! {CARD_WS} に履歴がありません。先に daily を走らせてください。")
            return 1
        log(f"=== 銘柄照会: '{CARD_QUERY}' (履歴 {len(card_rows)}行から検索) ===")
        if report_card(card_rows, CARD_QUERY, SHOW_DAYS) == 0:
            return 1

        # ログだけでなくシートにも転記する
        cards = []
        seen = set()
        for r in card_rows:
            cid = r.get("card_id", "")
            if cid and cid not in seen:
                seen.add(cid)
                cards.append({"card_id": cid, "card_name": r.get("card_name", ""),
                              "hinban": r.get("hinban", "")})
        hit_ids = {c["card_id"] for c in match_cards(CARD_QUERY, cards)[:5]}
        status = build_status_rows(card_rows, only_ids=hit_ids)
        if status:
            log("")
            log(f"--- 現況ボードへ転記: {len(status)}行 ---")
            upsert_to_sheet(sh, STATUS_WS, STATUS_HEADERS, status, STATUS_KEY_FIELDS, dry_run)
        log("")
        log("=== 完了 ===")
        return 0

    # --- watchlist はマスタと突き合わせて card_id / url を埋めるだけ ---
    if MODE == "watchlist":
        master = read_history(sh, MASTER_WS, MASTER_HEADERS)
        if not master:
            log(f"! {MASTER_WS} が空です。先に PKC_MODE=master を走らせてください。")
            return 1
        watch = load_watchlist(sh)
        if not watch:
            log(f"! {WATCHLIST_WS} が空です。品番と名前を手入力してください。")
            _get_or_create(sh, WATCHLIST_WS, WATCHLIST_HEADERS) if not dry_run else None
            return 0
        log(f"=== ウォッチリストの照合: {len(watch)}件 ===")
        resolved = resolve_watchlist(sh, watch, master)
        upsert_to_sheet(sh, WATCHLIST_WS, WATCHLIST_HEADERS, resolved,
                        WATCHLIST_KEY_FIELDS, dry_run)
        unresolved = [r for r in resolved if not r.get("card_id")]
        if unresolved:
            log(f"! {len(unresolved)}件が未解決です。{WATCHLIST_WS} の card_id / url を")
            log("  手で埋めてください。埋めた行は次回以降そのまま使われます。")
        log("=== 完了 ===")
        return 0

    # --- trend は蓄積済みデータだけで動く (サイトへアクセスしない) ---
    if MODE == "trend":
        index_rows = read_history(sh, INDEX_WS, INDEX_HEADERS)
        card_rows = read_history(sh, CARD_WS, CARD_HEADERS)
        log(f"  履歴: 指数 {len(index_rows)}行 / カード {len(card_rows)}行")
        trends = compute_all_trends(index_rows, card_rows)
        log_trend_summary(trends)
        upsert_to_sheet(sh, TREND_WS, TREND_HEADERS, trends, TREND_KEY_FIELDS, dry_run)
        log("=== 完了 ===")
        return 0

    if MODE == "analysis":
        run_market_analysis(sh, dry_run)
        log("=== 完了 ===")
        return 0

    # --- 以降はサイトへアクセスする。robots.txt を必ず確認する ---
    targets = [URLS["index"], URLS["all_card"]]
    if not assert_robots_allowed(targets):
        return 1

    if not CONTACT:
        log("! PKC_CONTACT が未設定です。相手先は個人運営の無料ファンサイトなので、")
        log("  連絡先を User-Agent に含めることを強く推奨します (リポジトリ変数で設定)。")

    from playwright.sync_api import sync_playwright

    index_records: list[dict] = []
    card_records: list[dict] = []
    master_records: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = _new_page(browser)
        recorder = NetworkRecorder()
        recorder.attach(page)
        try:
            if MODE in ("index", "daily", "backfill"):
                log("--- 指数の取得 ---")
                # 通常モードと鑑定特化モードの両方を取る
                index_records += fetch_index(page, recorder, appraisal=False)
                time.sleep(REQUEST_DELAY)
                index_records += fetch_index(page, recorder, appraisal=True)
                log(f"  指数 {len(index_records)}行")

            if MODE in ("master", "cards", "daily", "backfill"):
                log("--- カード一覧 (マスタ) の取得 ---")
                master_records = fetch_master(page, recorder)

            if MODE in ("cards", "daily", "backfill"):
                all_cards = master_records or [
                    {"card_id": r.get("card_id", ""), "card_name": r.get("card_name", ""),
                     "hinban": r.get("hinban", ""), "url": r.get("url", "")}
                    for r in read_history(sh, MASTER_WS, MASTER_HEADERS)
                ]

                # ★品番を入れた銘柄だけを巡回する。
                # 1件あたり REQUEST_DELAY 秒待つため全カード巡回は現実的に終わらず、
                # 相手先(個人運営の無料ファンサイト)への負荷も過大になる。
                watch = load_watchlist(sh)
                if watch:
                    watch = resolve_watchlist(sh, watch, all_cards)
                    if not dry_run:
                        upsert_to_sheet(sh, WATCHLIST_WS, WATCHLIST_HEADERS, watch,
                                        WATCHLIST_KEY_FIELDS, dry_run)
                    wanted = {w["card_id"] for w in watch if w.get("card_id")}
                    cards = [c for c in all_cards if c.get("card_id") in wanted]
                    # マスタに無いがURLを手入力した行も巡回対象にする
                    known = {c.get("card_id") for c in cards}
                    for w in watch:
                        if w.get("url") and w.get("card_id") not in known:
                            cards.append({
                                "card_id": w.get("card_id") or slug_from_url(w["url"]),
                                "card_name": w.get("名前", ""),
                                "hinban": w.get("品番", ""),
                                "url": w["url"],
                            })
                    log(f"  ウォッチリスト {len(watch)}件 -> 巡回対象 {len(cards)}件")
                    unresolved = [w for w in watch if not w.get("card_id") and not w.get("url")]
                    if unresolved:
                        log(f"  ! {len(unresolved)}件は照合できず巡回対象から外れています。")
                        for w in unresolved[:10]:
                            log(f"      {w.get('品番','')} {w.get('名前','')}")
                        log(f"    {WATCHLIST_WS} の card_id / url を手で埋めてください。")
                elif CRAWL_ALL:
                    cards = all_cards
                    log(f"  PKC_CRAWL_ALL=1 のため全 {len(cards)}件が対象です。")
                    log(f"  ! 1件あたり{REQUEST_DELAY}秒待つため、件数が多いと時間内に終わりません。")
                else:
                    # ★空のまま全件クロールに落とさない。相手先への負荷が大きすぎる。
                    log(f"! {WATCHLIST_WS} シートが空です。カード別価格の取得をスキップします。")
                    log(f"  巡回したい銘柄の品番を {WATCHLIST_WS} に入力してください。")
                    log("  (名前は任意。品番だけで照合できます)")
                    log("  全カードを巡回したい場合のみ PKC_CRAWL_ALL=1 を指定してください。")
                    if not dry_run:
                        _get_or_create(sh, WATCHLIST_WS, WATCHLIST_HEADERS)
                    cards = []

                cards = [c for c in cards if c.get("url")]
                if MAX_ITEMS > 0:
                    cards = cards[:MAX_ITEMS]
                # backfill は過去系列を丸ごと、daily は直近30日で足りる
                days = 3650 if MODE == "backfill" else 30
                log(f"--- カード別価格の取得: {len(cards)}件 (取得日数 {days}) ---")
                for i, card in enumerate(cards, 1):
                    log(f"[{i}/{len(cards)}] {card.get('card_name') or card['card_id']}")
                    try:
                        recs = fetch_card(page, recorder, card, days=days)
                    except Exception as exc:
                        log(f"    ! 例外: {exc}")
                        recs = []
                    if not recs:
                        log("    ! 系列を抽出できませんでした。")
                    card_records += recs
                    time.sleep(REQUEST_DELAY)
        finally:
            browser.close()

    if index_records:
        upsert_to_sheet(sh, INDEX_WS, INDEX_HEADERS, index_records, INDEX_KEY_FIELDS, dry_run)
    if master_records:
        upsert_to_sheet(sh, MASTER_WS, MASTER_HEADERS, master_records, MASTER_KEY_FIELDS, dry_run)
    if card_records:
        upsert_to_sheet(sh, CARD_WS, CARD_HEADERS, card_records, CARD_KEY_FIELDS, dry_run)

    # --- 対象カードの現況ボードをシートへ転記する ---
    if MODE in ("cards", "daily", "backfill"):
        history = read_history(sh, CARD_WS, CARD_HEADERS) if not dry_run else card_records
        watch_ids = {w.get("card_id") for w in load_watchlist(sh) if w.get("card_id")}
        # 品番を入れた銘柄だけを載せる (ウォッチリストが空なら履歴にあるもの全部)
        status = build_status_rows(history, only_ids=watch_ids or None)
        if status:
            log(f"--- 現況ボードの更新: {len(status)}行 ---")
            upsert_to_sheet(sh, STATUS_WS, STATUS_HEADERS, status, STATUS_KEY_FIELDS, dry_run)
        update_watchlist_psa10(sh, history, dry_run)

    # --- daily は最後に傾向を再計算する (これが出力の本体) ---
    if MODE in ("daily", "backfill"):
        log("--- 傾向の再計算 ---")
        index_rows = read_history(sh, INDEX_WS, INDEX_HEADERS) if not dry_run else index_records
        card_rows = read_history(sh, CARD_WS, CARD_HEADERS) if not dry_run else card_records
        trends = compute_all_trends(index_rows, card_rows)
        log_trend_summary(trends)
        upsert_to_sheet(sh, TREND_WS, TREND_HEADERS, trends, TREND_KEY_FIELDS, dry_run)

        log("--- 1〜3か月市場分析 ---")
        run_market_analysis(sh, dry_run)

    log("=== 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())


#!/usr/bin/env python3
"""
スニダン (SNKRDUNK) 相場スクレイパー

既存の torecabank スクレイパー (scraper/scrape.py) とは
**完全に独立したプロセス** として動作する。
- scrape.py を import しない
- 環境変数はすべて SNKR_ プレフィックス (共有は下記2つのみ)
- 書き込み先シートも別 (bank シートには一切触れない)

--------------------------------------------------------------------------
なぜ 2 フェーズ構成なのか
--------------------------------------------------------------------------
torecabank は「買取リスト一覧を『次へ』で送るだけで全件取れる」構造だったが、
スニダンには全銘柄が価格付きで並ぶ一覧ページが無く、商品ページを1件ずつ
開く必要がある。毎回「検索して商品URLを特定」までやるとアクセス数が倍になり
失敗率も上がるため、解決結果をシートにキャッシュする。

  [解決フェーズ] 品番+名前で検索 → 商品URLを特定 → snkr_map シートへ保存
                 ※ 一度URLが入った行は二度と検索しない (空欄の行だけ処理)
  [巡回フェーズ] snkr_map の解決済みURLだけを開いて数値を取得 → snkr シートへ

--------------------------------------------------------------------------
環境変数
--------------------------------------------------------------------------
既存 scrape.py と共有してよいもの (この2つだけ):
  GOOGLE_SERVICE_ACCOUNT_JSON  サービスアカウント鍵 (生JSON / base64 どちらも可)
  SPREADSHEET_ID               書き込み先スプレッドシートID

このスクリプト専用 (既存の WORKSHEET_NAME / REQUEST_DELAY / MAX_PAGES /
BASE_URL は **絶対に読まない**。リポジトリ変数で bank 側の挙動が変わるため):
  SNKR_MODE            probe / resolve / poll / all   (既定: all)
  SNKR_MAP_WORKSHEET   マッピングシート名             (既定: snkr_map)
  SNKR_DATA_WORKSHEET  数値シート名                   (既定: snkr)
  SNKR_REQUEST_DELAY   1件ごとの待機秒                (既定: 3.0)
  SNKR_MAX_ITEMS       各フェーズの処理件数上限 0=無制限 (既定: 0)
  SNKR_PROBE_URL       probe モードで開く商品URL

  DRY_RUN=1            スプレッドシートへ書き込まずログ出力のみ (ワークフローで明示指定)
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

# ===========================================================================
# 【要設定】セレクタ / URL 設定ブロック
# ---------------------------------------------------------------------------
# スニダンの DOM は未確認のため、ここの値は「候補」である。
# probe モード (SNKR_MODE=probe) で実ページを保存し、
# probe_page.html / probe_text.txt を見て確定させること。
#
# 各リストは「上から順に試して最初にヒットしたものを採用」する。
# 全滅した場合は本文テキストの正規表現フォールバック (下部) が使われる。
# ===========================================================================

SNKR_ORIGIN = "https://snkrdunk.com"

# --- 検索URL候補 (要設定) --------------------------------------------------
# {q} に URL エンコード済みの検索キーが入る。
SEARCH_URL_TEMPLATES = [
    "https://snkrdunk.com/search?keyword={q}",
    "https://snkrdunk.com/search?q={q}",
]

# --- 検索結果から商品リンクを拾うセレクタ候補 (要設定) ---------------------
SEARCH_RESULT_LINK_SELECTORS = [
    "a[href*='/trading-cards/']",
    "a[href*='/products/']",
    "[class*='searchResult'] a[href]",
    "[class*='product'] a[href]",
]

# 商品URLとして採用してよい href のパターン (誤爆防止)
PRODUCT_URL_PATTERNS = [
    r"/trading-cards/\d+",
    r"/products/\d+",
    r"/trading-cards/[a-z0-9\-]+/\d+",
]

# --- 商品ページの数値セレクタ候補 (要設定) ---------------------------------
# ここが今回いちばん人間の確認が要る箇所。
PRICE_SELECTORS = {
    # 最安出品価格
    "最安価格": [
        "[class*='lowestPrice']",
        "[class*='lowest-price']",
        "[class*='minPrice']",
        "[data-testid*='lowest']",
    ],
    # 出品枚数 (薄い銘柄だと1〜2枚で乱高下するため必ず併記する)
    "出品枚数": [
        "[class*='askCount']",
        "[class*='listingCount']",
        "[class*='stockCount']",
        "[data-testid*='listing']",
    ],
    # 直近取引価格
    "直近取引価格": [
        "[class*='lastSoldPrice']",
        "[class*='latestPrice']",
        "[class*='recentPrice']",
        "[data-testid*='last-sold']",
    ],
}

# --- セレクタ全滅時の本文テキスト正規表現フォールバック (要設定) -----------
TEXT_FALLBACK_PATTERNS = {
    "最安価格": [
        r"最安値?[^0-9¥￥]{0,20}[¥￥]?\s*([0-9][0-9,]*)",
        r"最低価格[^0-9¥￥]{0,20}[¥￥]?\s*([0-9][0-9,]*)",
        r"購入[^0-9¥￥]{0,10}[¥￥]\s*([0-9][0-9,]*)",
    ],
    "出品枚数": [
        r"([0-9][0-9,]*)\s*(?:点|枚|件)\s*の?出品",
        r"出品[数枚点件][^0-9]{0,10}([0-9][0-9,]*)",
        r"在庫[^0-9]{0,10}([0-9][0-9,]*)",
    ],
    "直近取引価格": [
        r"直近[^0-9¥￥]{0,20}[¥￥]?\s*([0-9][0-9,]*)",
        r"最終取引価格[^0-9¥￥]{0,20}[¥￥]?\s*([0-9][0-9,]*)",
        r"直近の?取引価格[^0-9¥￥]{0,20}[¥￥]?\s*([0-9][0-9,]*)",
    ],
}

# 商品ページの描画完了待ちに使うセレクタ候補 (要設定)
PRODUCT_READY_SELECTORS = [
    "[class*='lowestPrice']",
    "[class*='price']",
    "main",
]

# ===========================================================================
# 設定 (環境変数)
# ===========================================================================
DEFAULT_SPREADSHEET_ID = "1XZQO4j7gu-p9IsK3sfaQp4q9O2bH893C2PMv2h_xqzE"
DEFAULT_MAP_WORKSHEET = "snkr_map"
DEFAULT_DATA_WORKSHEET = "snkr"

MODE = (os.environ.get("SNKR_MODE") or "all").strip().lower()
REQUEST_DELAY = float(os.environ.get("SNKR_REQUEST_DELAY") or "3.0")
MAX_ITEMS = int(os.environ.get("SNKR_MAX_ITEMS") or "0")  # 0 = 無制限
PROBE_URL = (os.environ.get("SNKR_PROBE_URL") or "").strip()

MAP_HEADERS = ["品番", "名前", "検索キー", "snkr_url", "解決日時", "備考"]
DATA_HEADERS = ["品番", "名前", "最安価格", "出品枚数", "直近取引価格", "取得日時"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(msg, flush=True)


def now_jst() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# 名寄せ (正規化)
# ---------------------------------------------------------------------------
# 買取表の実データには以下の揺れが混ざっている。突合前に必ず正規化する。
#   211/SMｰP        ハイフンが半角カナ長音 U+FF70 (他行の - と別文字)
#   296/XY-p        末尾が小文字
#   "206/165 "      末尾スペース (かつ名前が空)
#   ガブリアス＆ギラティナＧＸ   全角英字
#   "ポンチョを着たピカチュウ　　(…)"  全角スペースの連続
#   "オリジンパルキアV(SA)\n\n"  セル内改行
#   ブラッキーVMax（高い方）     自店メモが名前に混入
#
# ★必ず踏むバグ (要注意)
#   品番の U+FF70 は NFKC で U+30FC (カタカナ長音「ー」) になる。
#   品番側ではこれを '-' に統一する必要があるが、**同じ変換を名前に適用すると
#   語が壊れる**:
#       ルイージピカチュウ -> ルイ-ジピカチュウ   ← 事故
#       ブラッキーVMax     -> ブラッキ-VMax       ← 事故
#       リーリエの決心     -> リ-リエの決心       ← 事故
#   そのため、ハイフン文字セットを品番用と名前用で必ず分ける。
#     品番用: U+FF70 / U+30FC を含めてよい (品番にカタカナは出ない)
#     名前用: U+FF70 / U+30FC を絶対に含めない
# ===========================================================================

# 名前にも品番にも共通して '-' に寄せてよいハイフン類 (長音記号は含めない)
COMMON_HYPHENS = (
    "-"  # - HYPHEN-MINUS
    "­"  # SOFT HYPHEN
    "‐"  # ‐ HYPHEN
    "‑"  # ‑ NON-BREAKING HYPHEN
    "‒"  # ‒ FIGURE DASH
    "–"  # – EN DASH
    "—"  # — EM DASH
    "―"  # ― HORIZONTAL BAR
    "⁃"  # ⁃ HYPHEN BULLET
    "−"  # − MINUS SIGN
    "－"  # － FULLWIDTH HYPHEN-MINUS (NFKC で U+002D になるが念のため)
)

# 品番専用。長音記号 2 種を追加する (品番にカタカナは出ないので安全)
CODE_HYPHENS = COMMON_HYPHENS + (
    "ー"  # ー KATAKANA-HIRAGANA PROLONGED SOUND MARK
    "ｰ"  # ｰ HALFWIDTH KATAKANA PROLONGED SOUND MARK
)

_CODE_HYPHEN_RE = re.compile("[" + re.escape(CODE_HYPHENS) + "]")
_NAME_HYPHEN_RE = re.compile("[" + re.escape(COMMON_HYPHENS) + "]")

# 空白扱いする文字 (全角スペース U+3000 / NBSP / セル内改行 / タブ)
_SPACE_RE = re.compile(r"[\s　 ]+")

# 自店メモ。カードの正式表記 (SA) (UR) などを消さないよう、語を限定列挙する。
STORE_MEMO_WORDS = [
    "高い方",
    "安い方",
    "高いほう",
    "安いほう",
    "高値",
    "安値",
    "要確認",
    "在庫僅少",
    "自店",
]
_STORE_MEMO_RE = re.compile(
    r"[（(\[【]?\s*(?:" + "|".join(re.escape(w) for w in STORE_MEMO_WORDS) + r")\s*[)）\]】]?"
)


def normalize_code(raw: str) -> str:
    """品番の正規化。

    NFKC → 長音記号を含むハイフン類を '-' に統一 → 空白除去 → 大文字化。

      "211/SMｰP"  -> "211/SM-P"   (U+FF70 経由)
      "211/SM-P"  -> "211/SM-P"
      "296/XY-p"  -> "296/XY-P"
      "206/165 "  -> "206/165"
    """
    s = unicodedata.normalize("NFKC", raw or "")
    s = _CODE_HYPHEN_RE.sub("-", s)
    s = _SPACE_RE.sub("", s)  # 品番の内部空白は意味を持たないので全除去
    return s.upper()


def normalize_name(raw: str) -> str:
    """名前の正規化。

    NFKC (全角英字・全角スペースを吸収) → **長音記号を含まない** ハイフン類のみ
    '-' に統一 → 連続空白/セル内改行を半角スペース1つへ → 前後トリム。

    長音記号を触らないのが肝。ここで CODE_HYPHENS を使うと
    「ルイージ」「ブラッキー」「リーリエ」が壊れる。
    """
    s = unicodedata.normalize("NFKC", raw or "")
    s = _NAME_HYPHEN_RE.sub("-", s)
    s = _SPACE_RE.sub(" ", s)
    return s.strip()


def strip_store_memo(name: str) -> str:
    """名前に混入した自店メモ (（高い方） / 安い方 など) を除去する。"""
    s = _STORE_MEMO_RE.sub(" ", name or "")
    # メモを抜いた結果できた空括弧・余分な空白を掃除
    s = re.sub(r"[（(\[【]\s*[)）\]】]", " ", s)
    s = _SPACE_RE.sub(" ", s)
    return s.strip()


def _code_view(text: str) -> str:
    """「名前の中に品番が既に含まれているか」を判定するための比較専用ビュー。

    品番と同じハイフン規則を当てるため語が壊れるが、比較にしか使わないので安全。
    表示や検索キーには絶対に使わないこと。
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = _CODE_HYPHEN_RE.sub("-", s)
    return s.upper()


def build_search_key(code: str, name: str) -> str:
    """検索キーを生成する。

    - 品番・名前をそれぞれ正規化
    - 名前から自店メモを除去
    - 名前に品番が既に含まれる場合は品番を前置しない (重複回避)
    """
    c = normalize_code(code)
    n = strip_store_memo(normalize_name(name))

    if not c:
        return n
    if not n:
        return c
    if c in _code_view(n):
        return n
    return f"{c} {n}"


def find_duplicate_codes(codes) -> set:
    """重複している品番の集合を返す (データ側の誤り。備考で人間に返す)。"""
    seen, dup = set(), set()
    for raw in codes:
        c = normalize_code(raw)
        if not c:
            continue
        if c in seen:
            dup.add(c)
        seen.add(c)
    return dup


def parse_number(text: str) -> str:
    """テキストから数字のみを取り出す ("¥12,800" -> "12800")。"""
    return re.sub(r"[^0-9]", "", text or "")


# ===========================================================================
# Playwright 共通
# ===========================================================================
def _new_browser(p):
    return p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])


def _new_page(browser):
    return browser.new_page(
        user_agent=USER_AGENT,
        locale="ja-JP",
        viewport={"width": 1366, "height": 900},
    )


def _wait_any(page, selectors, timeout_ms: int = 15000) -> str | None:
    """候補セレクタを上から試し、最初に現れたものを返す。全滅なら None。"""
    from playwright.sync_api import TimeoutError as PWTimeout

    per = max(2000, timeout_ms // max(1, len(selectors)))
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=per, state="attached")
            return sel
        except PWTimeout:
            continue
        except Exception:
            continue
    return None


def _first_text(page, selectors) -> tuple[str, str]:
    """候補セレクタを上から試し、(採用セレクタ, テキスト) を返す。全滅なら ("","")。"""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            continue
        if not el:
            continue
        try:
            txt = (el.inner_text() or "").strip()
        except Exception:
            continue
        if parse_number(txt):
            return sel, txt
    return "", ""


def _from_text_patterns(body_text: str, patterns) -> str:
    """本文テキストへの正規表現フォールバック。"""
    for pat in patterns:
        m = re.search(pat, body_text or "")
        if m:
            return parse_number(m.group(1))
    return ""


def _body_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


# ===========================================================================
# probe モード: 実ページを保存してセレクタを確定させるための調査
# ===========================================================================
def run_probe(url: str) -> int:
    from playwright.sync_api import sync_playwright

    if not url:
        log("ERROR: SNKR_PROBE_URL が未設定です。調査したい商品ページのURLを指定してください。")
        log("  例: SNKR_MODE=probe SNKR_PROBE_URL='https://snkrdunk.com/trading-cards/123456' \\")
        log("      DRY_RUN=1 python scraper/snkr_scrape.py")
        return 1

    log(f"=== probe モード: {url} ===")
    with sync_playwright() as p:
        browser = _new_browser(p)
        page = _new_page(browser)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            hit = _wait_any(page, PRODUCT_READY_SELECTORS, timeout_ms=20000)
            log(f"  描画待ちセレクタ: {hit or '(全滅 — 待機なしで続行)'}")
            time.sleep(REQUEST_DELAY)

            html = page.content()
            text = _body_text(page)
            with open("probe_page.html", "w", encoding="utf-8") as f:
                f.write(html)
            with open("probe_text.txt", "w", encoding="utf-8") as f:
                f.write(text)
            page.screenshot(path="probe_screenshot.png", full_page=True)
            log("  probe_page.html / probe_text.txt / probe_screenshot.png を保存しました。")

            # 現在のセレクタ候補のヒット状況を全部ログに出す
            log("--- セレクタ候補のヒット状況 ---")
            for label, selectors in PRICE_SELECTORS.items():
                log(f"[{label}]")
                for sel in selectors:
                    try:
                        els = page.query_selector_all(sel)
                    except Exception as exc:
                        log(f"  x {sel}  (セレクタ評価エラー: {exc})")
                        continue
                    if not els:
                        log(f"  x {sel}  ヒット0")
                        continue
                    sample = ""
                    try:
                        sample = (els[0].inner_text() or "").strip().replace("\n", " / ")[:80]
                    except Exception:
                        pass
                    log(f"  o {sel}  ヒット{len(els)}件  例: {sample!r}")

                log(f"  -- 本文テキスト正規表現フォールバック [{label}] --")
                for pat in TEXT_FALLBACK_PATTERNS[label]:
                    m = re.search(pat, text)
                    log(f"    {'o' if m else 'x'} {pat}  → {m.group(1) if m else '(不一致)'}")

            log("--- 商品リンクセレクタ候補 (検索結果ページで使うもの) ---")
            for sel in SEARCH_RESULT_LINK_SELECTORS:
                try:
                    n = len(page.query_selector_all(sel))
                except Exception:
                    n = -1
                log(f"  {'o' if n > 0 else 'x'} {sel}  ヒット{n}件")

            log("※ 上記が全滅なら probe_page.html / probe_text.txt を読み、")
            log("  ファイル冒頭の PRICE_SELECTORS / TEXT_FALLBACK_PATTERNS を書き換えてください。")
            return 0
        except Exception as exc:
            import traceback

            log(f"! probe 中に例外: {exc}")
            log(traceback.format_exc())
            try:
                with open("probe_page.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                page.screenshot(path="probe_screenshot.png", full_page=True)
            except Exception:
                pass
            return 1
        finally:
            browser.close()


# ===========================================================================
# 解決フェーズ: 検索キー → 商品URL
# ===========================================================================
def _is_product_url(href: str) -> bool:
    return any(re.search(pat, href or "") for pat in PRODUCT_URL_PATTERNS)


def _extract_product_link(html: str, page) -> str:
    """検索結果ページから商品URLを1件取り出す。

    セレクタ候補 → 全滅時は全 <a> の href パターン照合、の順で試す。
    """
    from bs4 import BeautifulSoup

    for sel in SEARCH_RESULT_LINK_SELECTORS:
        try:
            el = page.query_selector(sel)
        except Exception:
            continue
        if not el:
            continue
        href = el.get_attribute("href") or ""
        if _is_product_url(href):
            return urljoin(SNKR_ORIGIN, href)

    # フォールバック: HTML中の全リンクをパターン照合
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        if _is_product_url(a["href"]):
            return urljoin(SNKR_ORIGIN, a["href"])
    return ""


def resolve_one(page, search_key: str) -> str:
    """検索キー1件から商品URLを解決する。見つからなければ空文字。"""
    for tmpl in SEARCH_URL_TEMPLATES:
        url = tmpl.format(q=quote(search_key))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            log(f"    検索ページを開けませんでした ({url}): {exc}")
            continue

        _wait_any(page, SEARCH_RESULT_LINK_SELECTORS, timeout_ms=12000)
        time.sleep(REQUEST_DELAY)

        try:
            html = page.content()
        except Exception:
            html = ""
        link = _extract_product_link(html, page)
        if link:
            return link
    return ""


def run_resolve(rows: list[dict], dry_run: bool) -> int:
    """snkr_url が空の行だけ検索する。解決済みの行は二度と触らない。"""
    from playwright.sync_api import sync_playwright

    targets = [r for r in rows if not r["snkr_url"].strip() and r["検索キー"]]
    if MAX_ITEMS > 0:
        targets = targets[:MAX_ITEMS]

    log(f"=== 解決フェーズ: 未解決 {len([r for r in rows if not r['snkr_url'].strip()])} 行 "
        f"/ 今回処理 {len(targets)} 行 ===")
    if not targets:
        return 0

    resolved = 0
    with sync_playwright() as p:
        browser = _new_browser(p)
        page = _new_page(browser)
        try:
            for i, row in enumerate(targets, 1):
                key = row["検索キー"]
                log(f"[{i}/{len(targets)}] 検索: {key}")
                try:
                    link = resolve_one(page, key)
                except Exception as exc:
                    log(f"    ! 例外: {exc}")
                    link = ""

                if link:
                    row["snkr_url"] = link
                    row["解決日時"] = now_jst()
                    row["備考"] = _drop_note(row["備考"], "検索ヒットなし")
                    resolved += 1
                    log(f"    → {link}")
                else:
                    row["備考"] = _add_note(row["備考"], "検索ヒットなし")
                    log("    → ヒットなし (snkr_map の snkr_url を手で埋めてください)")
                time.sleep(REQUEST_DELAY)
        finally:
            browser.close()

    log(f"=== 解決フェーズ完了: {resolved}/{len(targets)} 件 解決 ===")
    return resolved


# ===========================================================================
# 巡回フェーズ: 商品URL → 数値3点
# ===========================================================================
def poll_one(page, url: str) -> dict:
    """商品ページから 最安価格 / 出品枚数 / 直近取引価格 を取る。"""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _wait_any(page, PRODUCT_READY_SELECTORS, timeout_ms=20000)
    time.sleep(REQUEST_DELAY)

    body = _body_text(page)
    result = {}
    for label, selectors in PRICE_SELECTORS.items():
        sel, txt = _first_text(page, selectors)
        value = parse_number(txt)
        if not value:
            # セレクタ全滅 → 本文テキスト正規表現フォールバック
            value = _from_text_patterns(body, TEXT_FALLBACK_PATTERNS[label])
            sel = "(本文テキスト正規表現)" if value else ""
        result[label] = value
        log(f"    {label}: {value or '(取得失敗)'}  [{sel or 'セレクタ全滅'}]")
    return result


def run_poll(rows: list[dict], dry_run: bool) -> list[dict]:
    """解決済みURLだけを巡回して数値を取得する。"""
    from playwright.sync_api import sync_playwright

    targets = [r for r in rows if r["snkr_url"].strip()]
    if MAX_ITEMS > 0:
        targets = targets[:MAX_ITEMS]

    log(f"=== 巡回フェーズ: 解決済み {len([r for r in rows if r['snkr_url'].strip()])} 行 "
        f"/ 今回処理 {len(targets)} 行 ===")

    records: list[dict] = []
    if not targets:
        return records

    fail = 0
    with sync_playwright() as p:
        browser = _new_browser(p)
        page = _new_page(browser)
        try:
            for i, row in enumerate(targets, 1):
                log(f"[{i}/{len(targets)}] {row['品番']} {row['名前']}")
                try:
                    vals = poll_one(page, row["snkr_url"].strip())
                except Exception as exc:
                    log(f"    ! 例外: {exc}")
                    vals = {k: "" for k in PRICE_SELECTORS}

                if not any(vals.values()):
                    fail += 1
                records.append(
                    {
                        "品番": row["品番"],
                        "名前": row["名前"],
                        "最安価格": vals.get("最安価格", ""),
                        "出品枚数": vals.get("出品枚数", ""),
                        "直近取引価格": vals.get("直近取引価格", ""),
                    }
                )
        finally:
            browser.close()

    log(f"=== 巡回フェーズ完了: {len(records)} 件 (数値が1つも取れなかった行: {fail}) ===")
    if fail and fail == len(records):
        log("! 全件で数値が取れていません。セレクタが変わった可能性があります。")
        log("  SNKR_MODE=probe で商品ページを調査し、PRICE_SELECTORS を更新してください。")
    return records


# ===========================================================================
# 備考の付け外し
# ===========================================================================
def _add_note(note: str, text: str) -> str:
    parts = [p.strip() for p in (note or "").split("/") if p.strip()]
    if text not in parts:
        parts.append(text)
    return " / ".join(parts)


def _drop_note(note: str, text: str) -> str:
    parts = [p.strip() for p in (note or "").split("/") if p.strip() and p.strip() != text]
    return " / ".join(parts)


# ===========================================================================
# Google Sheets
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
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ.get("SPREADSHEET_ID") or DEFAULT_SPREADSHEET_ID)


def _col_letter(n: int) -> str:
    """1 -> A, 27 -> AA。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def load_map(sh) -> tuple[list[dict], list[str], object]:
    """snkr_map を読み込む。

    列は **ヘッダー名で解決** する。人間が追加した列があっても壊さないよう、
    シートの生の行データもそのまま保持する。
    """
    import gspread

    name = os.environ.get("SNKR_MAP_WORKSHEET") or DEFAULT_MAP_WORKSHEET
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=200, cols=len(MAP_HEADERS))
        ws.update([MAP_HEADERS], "A1", value_input_option="USER_ENTERED")
        log(f"シート '{name}' を新規作成しました。品番/名前を手入力してください。")
        return [], list(MAP_HEADERS), ws

    values = ws.get_all_values()
    if not values:
        ws.update([MAP_HEADERS], "A1", value_input_option="USER_ENTERED")
        return [], list(MAP_HEADERS), ws

    header = list(values[0])
    # 必須列が無ければ末尾に追加する (既存の手入力列は動かさない)
    for h in MAP_HEADERS:
        if h not in header:
            header.append(h)
    width = len(header)
    idx = {h: header.index(h) for h in MAP_HEADERS}

    rows = []
    for raw in values[1:]:
        cells = list(raw) + [""] * (width - len(raw))
        cells = cells[:width]
        rows.append(
            {
                "cells": cells,
                "品番": cells[idx["品番"]],
                "名前": cells[idx["名前"]],
                "検索キー": cells[idx["検索キー"]],
                "snkr_url": cells[idx["snkr_url"]],
                "解決日時": cells[idx["解決日時"]],
                "備考": cells[idx["備考"]],
            }
        )
    return rows, header, ws


def prepare_rows(rows: list[dict]) -> None:
    """正規化・検索キー生成・品番重複の印付けを行う (シートへの書き戻し前)。"""
    dups = find_duplicate_codes(r["品番"] for r in rows)
    for r in rows:
        code = normalize_code(r["品番"])
        # 名前は自店メモを残したまま正規化する (高い方/安い方 の行を区別するため)
        name = normalize_name(r["名前"])
        r["品番"] = code
        r["名前"] = name
        r["検索キー"] = build_search_key(code, name)
        if code and code in dups:
            r["備考"] = _add_note(r["備考"], "品番重複（要確認）")
        else:
            r["備考"] = _drop_note(r["備考"], "品番重複（要確認）")
    if dups:
        log(f"! 品番が重複しています (データ側の誤り。備考に印を付けました): {sorted(dups)}")


def write_map(ws, rows: list[dict], header: list[str]) -> None:
    """snkr_map へ書き戻す。

    ★ clear() してはいけない。手で埋めた snkr_url が毎回消えるため。
      読み込んだのと同じ行数・同じ列構成のまま上書きする。
    """
    idx = {h: header.index(h) for h in MAP_HEADERS}
    grid = [list(header)]
    for r in rows:
        cells = list(r["cells"]) + [""] * (len(header) - len(r["cells"]))
        cells = cells[: len(header)]
        for key in MAP_HEADERS:
            cells[idx[key]] = r[key]
        grid.append(cells)

    rng = f"A1:{_col_letter(len(header))}{len(grid)}"
    ws.update(grid, rng, value_input_option="USER_ENTERED")
    log(f"snkr_map へ {len(rows)} 行を書き戻しました (clear なし / 既存値は保持)。")


def write_data(sh, records: list[dict]) -> None:
    """snkr (数値専用) へ書き込む。こちらは毎回 clear して全上書きしてよい。"""
    import gspread

    name = os.environ.get("SNKR_DATA_WORKSHEET") or DEFAULT_DATA_WORKSHEET
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=200, cols=len(DATA_HEADERS))

    timestamp = now_jst()
    grid = [DATA_HEADERS]
    for r in records:
        grid.append(
            [
                r["品番"],
                r["名前"],
                r["最安価格"],
                r["出品枚数"],
                r["直近取引価格"],
                timestamp,
            ]
        )

    ws.clear()
    ws.update(grid, value_input_option="USER_ENTERED")
    log(f"シート '{name}' へ {len(records)} 件書き込み完了。")


# ===========================================================================
# main
# ===========================================================================
def main() -> int:
    dry_run = os.environ.get("DRY_RUN") == "1"

    if MODE not in ("probe", "resolve", "poll", "all"):
        log(f"ERROR: SNKR_MODE が不正です: {MODE!r} (probe / resolve / poll / all)")
        return 1

    log(f"=== スニダン相場スクレイパー 開始 (mode={MODE}, dry_run={dry_run}) ===")

    if MODE == "probe":
        # probe は成果物をローカルに保存するだけ。シートには触らない。
        return run_probe(PROBE_URL)

    # 書き込みありなのに鍵が無い場合は、ブラウザを起動する前に明確に失敗させる
    if not dry_run and not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        log("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
        log("  リポジトリの Settings → Secrets and variables → Actions に登録してください。")
        return 1

    if dry_run and not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        # 鍵無しの DRY_RUN では snkr_map を読めないので何もできない
        log("ERROR: DRY_RUN でも snkr_map の読み込みに鍵が必要です。")
        log("  セレクタだけ確認したい場合は SNKR_MODE=probe を使ってください。")
        return 1

    sh = _open_spreadsheet()
    rows, header, map_ws = load_map(sh)
    log(f"snkr_map: {len(rows)} 行 読み込み")
    if not rows:
        log("! snkr_map にデータ行がありません。品番 / 名前 を手入力してください。")
        return 0

    prepare_rows(rows)

    if MODE in ("resolve", "all"):
        run_resolve(rows, dry_run)

    if dry_run:
        log("DRY_RUN=1 のため snkr_map への書き戻しはスキップします。サンプル:")
        for r in rows[:5]:
            log(f"  {r['品番']} | {r['名前']} | key={r['検索キー']} | url={r['snkr_url']} | {r['備考']}")
    else:
        write_map(map_ws, rows, header)

    if MODE in ("poll", "all"):
        records = run_poll(rows, dry_run)
        if not records:
            log("! 巡回対象がありません。snkr_map の snkr_url が空のままです。")
            return 0
        if dry_run:
            log("DRY_RUN=1 のため snkr への書き込みはスキップします。サンプル:")
            for r in records[:5]:
                log(f"  {r}")
        else:
            write_data(sh, records)

    log("=== 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

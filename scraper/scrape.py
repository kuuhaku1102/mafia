#!/usr/bin/env python3
"""
torecabank 買取リスト スクレイパー

https://store.torecabank.com/kaitori_list の全データを取得し、
Google スプレッドシートへ書き込む（毎日上書き）。

実行に必要な環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON  サービスアカウント鍵 (JSON文字列そのもの)
  SPREADSHEET_ID               書き込み先スプレッドシートのID
  WORKSHEET_NAME               シート(タブ)名 (省略時: "買取リスト")

任意の上書き用環境変数 (サイト構造に合わせて調整する場合):
  BASE_URL        既定: https://store.torecabank.com/kaitori_list
  ITEM_SELECTOR   各アイテムを囲むCSSセレクタ (例: ".item")
  NAME_SELECTOR   アイテム内の商品名セレクタ
  PRICE_SELECTOR  アイテム内の価格セレクタ
  IMAGE_SELECTOR  アイテム内の画像セレクタ
  MAX_PAGES       ページネーション探索の上限 (既定: 300)
  REQUEST_DELAY   各リクエスト間の待機秒 (既定: 1.0)

セレクタ未指定の場合は、価格(円/¥)を含む繰り返し要素を自動検出する。
"""

import json
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL", "https://store.torecabank.com/kaitori_list")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "300"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.0"))

ITEM_SELECTOR = os.environ.get("ITEM_SELECTOR") or None
NAME_SELECTOR = os.environ.get("NAME_SELECTOR") or None
PRICE_SELECTOR = os.environ.get("PRICE_SELECTOR") or None
IMAGE_SELECTOR = os.environ.get("IMAGE_SELECTOR") or None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# 価格らしき文字列の検出 (例: 1,200円 / ¥1,200 / 1200 円)
PRICE_RE = re.compile(r"(?:¥|￥)?\s*([0-9][0-9,]*)\s*円|(?:¥|￥)\s*([0-9][0-9,]*)")

# 自動検出で試す候補セレクタ (上から順に試し、最も多くヒットしたものを採用)
CANDIDATE_ITEM_SELECTORS = [
    ".kaitori-item", ".kaitori_item", ".kaitori-list-item",
    ".product-item", ".product", ".item-box", ".item-card",
    "li.item", ".item", ".card", ".goods", ".goods-item",
    ".list-item", "article", ".grid-item", ".col .card",
]


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def fetch(url: str, session: requests.Session) -> str | None:
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as exc:
        log(f"  ! リクエスト失敗 {url}: {exc}")
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        log(f"  ! HTTP {resp.status_code} {url}")
        return None
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


# ---------------------------------------------------------------------------
# パース
# ---------------------------------------------------------------------------
def find_price(text: str) -> str:
    m = PRICE_RE.search(text or "")
    if not m:
        return ""
    num = m.group(1) or m.group(2) or ""
    return num.replace(",", "")


def detect_item_selector(soup: BeautifulSoup) -> str | None:
    """価格を含む繰り返し要素から最適なセレクタを推定する。"""
    best_sel, best_count = None, 0
    for sel in CANDIDATE_ITEM_SELECTORS:
        nodes = soup.select(sel)
        if len(nodes) < 2:
            continue
        priced = sum(1 for n in nodes if find_price(n.get_text(" ", strip=True)))
        # 半分以上が価格を含むものを「アイテムらしい」とみなす
        if priced >= max(2, len(nodes) // 2) and priced > best_count:
            best_sel, best_count = sel, priced
    return best_sel


def text_of(node, selector: str | None) -> str:
    if selector:
        el = node.select_one(selector)
        return el.get_text(" ", strip=True) if el else ""
    return ""


def extract_name(node) -> str:
    if NAME_SELECTOR:
        return text_of(node, NAME_SELECTOR)
    # 画像のalt属性 → 最長のテキスト行 の順で名前らしきものを推定
    img = node.find("img")
    if img and img.get("alt"):
        alt = img.get("alt").strip()
        if alt:
            return alt
    candidates = [
        t.strip()
        for t in node.stripped_strings
        if t.strip() and not find_price(t)
    ]
    return max(candidates, key=len) if candidates else ""


def extract_price(node) -> str:
    if PRICE_SELECTOR:
        return find_price(text_of(node, PRICE_SELECTOR))
    return find_price(node.get_text(" ", strip=True))


def extract_image(node) -> str:
    el = node.select_one(IMAGE_SELECTOR) if IMAGE_SELECTOR else node.find("img")
    if not el:
        return ""
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        if el.get(attr):
            return urljoin(BASE_URL, el.get(attr))
    return ""


def extract_url(node) -> str:
    a = node.find("a", href=True)
    return urljoin(BASE_URL, a["href"]) if a else ""


def parse_page(html: str, selector: str | None) -> tuple[list[dict], str | None]:
    soup = BeautifulSoup(html, "lxml")
    if selector is None:
        selector = detect_item_selector(soup)
        if selector:
            log(f"  自動検出セレクタ: '{selector}'")
    if not selector:
        return [], None

    items = []
    for node in soup.select(selector):
        price = extract_price(node)
        name = extract_name(node)
        if not name and not price:
            continue
        items.append(
            {
                "商品名": name,
                "買取価格": price,
                "画像URL": extract_image(node),
                "詳細URL": extract_url(node),
            }
        )
    return items, selector


# ---------------------------------------------------------------------------
# ページネーション
# ---------------------------------------------------------------------------
def page_url(base: str, page: int) -> str:
    if page == 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}page={page}"


def scrape_all() -> list[dict]:
    session = requests.Session()
    all_items: list[dict] = []
    seen = set()
    selector = ITEM_SELECTOR
    last_dump = None

    for page in range(1, MAX_PAGES + 1):
        url = page_url(BASE_URL, page)
        log(f"[page {page}] {url}")
        html = fetch(url, session)
        if html is None:
            log("  ページ取得できず。終了。")
            break
        last_dump = html

        items, selector = parse_page(html, selector)
        if not items:
            log("  アイテム0件。最終ページとみなして終了。")
            break

        # 重複(同一URL/名前)で次ページが無いケースを検知して停止
        new_items = []
        for it in items:
            key = it["詳細URL"] or (it["商品名"], it["買取価格"])
            if key in seen:
                continue
            seen.add(key)
            new_items.append(it)

        if not new_items:
            log("  新規アイテムなし(同一ページの繰り返し)。終了。")
            break

        all_items.extend(new_items)
        log(f"  取得 {len(new_items)} 件 (累計 {len(all_items)} 件)")
        time.sleep(REQUEST_DELAY)

    # デバッグ用: 取得できなかった場合は最後のHTMLを保存
    if not all_items and last_dump:
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(last_dump)
        log("! アイテムを抽出できませんでした。debug_page.html を保存しました。")

    return all_items


# ---------------------------------------------------------------------------
# Google Sheets 書き込み (毎日上書き)
# ---------------------------------------------------------------------------
def write_to_sheets(items: list[dict]) -> None:
    import gspread
    from google.oauth2.service_account import Credentials

    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    worksheet_name = os.environ.get("WORKSHEET_NAME", "買取リスト")

    creds = Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=100, cols=10)

    headers = ["商品名", "買取価格", "画像URL", "詳細URL", "取得日時"]
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [headers]
    for it in items:
        rows.append(
            [
                it["商品名"],
                it["買取価格"],
                it["画像URL"],
                it["詳細URL"],
                timestamp,
            ]
        )

    ws.clear()
    ws.update(rows, value_input_option="USER_ENTERED")
    log(f"スプレッドシートへ {len(items)} 件書き込み完了 (シート: {worksheet_name})")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    log(f"=== スクレイピング開始: {BASE_URL} ===")
    items = scrape_all()
    log(f"=== 取得合計: {len(items)} 件 ===")

    if not items:
        log("ERROR: データを取得できませんでした。セレクタの調整が必要です。")
        return 1

    if os.environ.get("DRY_RUN") == "1":
        log("DRY_RUN=1 のためスプレッドシートへの書き込みはスキップします。")
        for it in items[:5]:
            log(f"  例: {it}")
        return 0

    write_to_sheets(items)
    return 0


if __name__ == "__main__":
    sys.exit(main())

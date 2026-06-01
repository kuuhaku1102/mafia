#!/usr/bin/env python3
"""
torecabank 買取リスト スクレイパー

https://store.torecabank.com/kaitori_list の全データを取得し、
Google スプレッドシートへ書き込む（毎日上書き）。

ページネーションが JavaScript 駆動 (href="javascript:void(0)") のため、
Playwright のヘッドレスブラウザで「次へ」をたどって全ページを巡回する。

実行に必要な環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON  サービスアカウント鍵 (JSON文字列そのもの)

任意の環境変数 (未指定時は下記デフォルトを使用):
  SPREADSHEET_ID               書き込み先スプレッドシートのID
  WORKSHEET_NAME               シート(タブ)名 (省略時: "bank")

任意:
  BASE_URL       既定: https://store.torecabank.com/kaitori_list
  MAX_PAGES      ページ巡回の上限 (既定: 100)
  REQUEST_DELAY  ページ遷移後の待機秒 (既定: 1.0)
  DRY_RUN=1      スプレッドシートへ書き込まずログ出力のみ
"""

import json
import os
import re
import sys
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE_URL = os.environ.get("BASE_URL", "https://store.torecabank.com/kaitori_list")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.0"))

# 書き込み先 (環境変数で上書き可能)
DEFAULT_SPREADSHEET_ID = "1XZQO4j7gu-p9IsK3sfaQp4q9O2bH893C2PMv2h_xqzE"
DEFAULT_WORKSHEET_NAME = "bank"

HEADERS = ["商品名", "グレード", "買取価格", "在庫", "受付状態", "画像URL", "取得日時"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# パース
# ---------------------------------------------------------------------------
def clean_url(url: str) -> str:
    """scheme以降の重複スラッシュを正規化 (例: com//uploads -> com/uploads)。"""
    url = (url or "").strip()
    if not url:
        return ""
    url = urljoin(BASE_URL, url)
    return re.sub(r"(?<!:)//+", "/", url)


def parse_price(text: str) -> str:
    digits = re.sub(r"[^0-9]", "", text or "")
    return digits


def _text(node, selector: str) -> str:
    el = node.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def parse_items(html: str) -> list[dict]:
    """1ページ分のHTMLから商品リストを抽出する。

    画像付きの #cardList を優先し、無ければ #listView を使う。
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("#cardList") or soup.select_one("#listView")
    if container is None:
        return []

    items = []
    for li in container.select("li.item"):
        name = _text(li, ".name")
        price = parse_price(_text(li, ".price"))
        if not name and not price:
            continue

        stock = _text(li, ".stock")
        classes = li.get("class", [])
        is_closed = "closed" in classes or "受付終了" in stock
        img = li.select_one(".card img")
        image = clean_url(img.get("src")) if img and img.get("src") else ""

        items.append(
            {
                "商品名": name,
                "グレード": _text(li, ".tag"),
                "買取価格": price,
                "在庫": stock,
                "受付状態": "受付終了" if is_closed else "募集中",
                "画像URL": image,
            }
        )
    return items


def expected_count(html: str) -> int | None:
    """#itemCount の "494件表示" から総件数を取得 (サニティチェック用)。"""
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one("#itemCount")
    if not el:
        return None
    digits = re.sub(r"[^0-9]", "", el.get_text())
    return int(digits) if digits else None


# ---------------------------------------------------------------------------
# Playwright で全ページ巡回
# ---------------------------------------------------------------------------
def scrape_all() -> list[dict]:
    from playwright.sync_api import sync_playwright

    all_items: list[dict] = []
    seen = set()
    seen_pages: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(
            user_agent=USER_AGENT,
            locale="ja-JP",
            viewport={"width": 1366, "height": 900},
        )
        try:
            _scrape_loop(page, all_items, seen, seen_pages)
        except Exception as exc:  # 失敗時はHTML/スクショを保存して原因調査できるように
            import traceback

            log(f"! スクレイピング中に例外: {exc}")
            log(traceback.format_exc())
            _dump_debug(page)
        finally:
            browser.close()

    if not all_items:
        log("! 抽出0件でした。debug_page.html / debug_screenshot.png を確認してください。")

    return all_items


def _dump_debug(page) -> None:
    try:
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path="debug_screenshot.png", full_page=True)
        log("  debug_page.html / debug_screenshot.png を保存しました。")
    except Exception as exc:
        log(f"  デバッグ情報の保存に失敗: {exc}")


def _scrape_loop(page, all_items, seen, seen_pages) -> None:
    from playwright.sync_api import TimeoutError as PWTimeout

    log(f"アクセス: {BASE_URL}")
    # networkidle は解析タグ等で確定しないことがあるため domcontentloaded を使う
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)

    # 商品リストの描画(AJAX)を待つ
    try:
        page.wait_for_selector("#cardList li.item, #listView li.item", timeout=45000)
    except PWTimeout:
        log("! 商品リストが表示されませんでした(タイムアウト)。")
        _dump_debug(page)

    total = expected_count(page.content())
    if total:
        log(f"総件数(itemCount): {total} 件")

    for n in range(1, MAX_PAGES + 1):
        cur = _current_page(page)
        html = page.content()

        page_items = parse_items(html)
        new_items = []
        for it in page_items:
            key = (it["商品名"], it["グレード"], it["買取価格"], it["画像URL"])
            if key in seen:
                continue
            seen.add(key)
            new_items.append(it)
        all_items.extend(new_items)
        log(f"[page {cur or n}] {len(page_items)} 件 (新規 {len(new_items)}, 累計 {len(all_items)})")

        seen_pages.add(cur or str(n))

        # 「次へ」ボタンの状態を確認
        next_btn = page.query_selector(".pagination .pager.next")
        if not next_btn:
            break
        cls = next_btn.get_attribute("class") or ""
        if "disabled" in cls:
            break

        next_btn.click()
        # 現在ページ番号が変わるまで待機
        try:
            page.wait_for_function(
                """(prev) => {
                    const el = document.querySelector('.pagination .page.current');
                    return el && el.innerText.trim() !== prev;
                }""",
                arg=(cur or ""),
                timeout=15000,
            )
        except PWTimeout:
            log("  次ページへの遷移を確認できませんでした。終了。")
            break

        if _current_page(page) in seen_pages:
            log("  既知のページに戻りました。終了。")
            break
        time.sleep(REQUEST_DELAY)

    if total and len(all_items) < total:
        log(f"! 注意: 取得 {len(all_items)} 件 < 総件数 {total} 件。巡回漏れの可能性。")


def _current_page(page) -> str | None:
    el = page.query_selector(".pagination .page.current")
    return el.inner_text().strip() if el else None


# ---------------------------------------------------------------------------
# Google Sheets 書き込み (毎日上書き)
# ---------------------------------------------------------------------------
def write_to_sheets(items: list[dict]) -> None:
    import gspread
    from google.oauth2.service_account import Credentials

    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    spreadsheet_id = os.environ.get("SPREADSHEET_ID") or DEFAULT_SPREADSHEET_ID
    worksheet_name = os.environ.get("WORKSHEET_NAME") or DEFAULT_WORKSHEET_NAME

    creds = Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=100, cols=len(HEADERS))

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [HEADERS]
    for it in items:
        rows.append(
            [
                it["商品名"],
                it["グレード"],
                it["買取価格"],
                it["在庫"],
                it["受付状態"],
                it["画像URL"],
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
    dry_run = os.environ.get("DRY_RUN") == "1"

    # 書き込みありなのに鍵が無い場合は、スクレイピング前に明確に失敗させる
    if not dry_run and not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        log("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
        log("  リポジトリの Settings → Secrets and variables → Actions に登録してください。")
        log("  (スクレイピングのみ確認したい場合は DRY_RUN=1 で実行)")
        return 1

    log(f"=== スクレイピング開始: {BASE_URL} ===")
    items = scrape_all()
    log(f"=== 取得合計: {len(items)} 件 ===")

    if not items:
        log("ERROR: データを取得できませんでした。debug_page.html / debug_screenshot.png を確認してください。")
        return 1

    if dry_run:
        log("DRY_RUN=1 のため書き込みはスキップします。サンプル:")
        for it in items[:5]:
            log(f"  {it}")
        return 0

    write_to_sheets(items)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""parse_items / parse_price のテスト (実HTMLのfixtureを使用)。"""
import importlib.util
import os

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "scrape", os.path.join(HERE, "..", "scrape.py")
)
scrape = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scrape)


def load_fixture():
    with open(os.path.join(HERE, "fixture_page.html"), encoding="utf-8") as f:
        return f.read()


def test_parse_items():
    html = load_fixture()
    items = scrape.parse_items(html)

    # #cardList の li.item は4件
    assert len(items) == 4, len(items)

    first = items[0]
    assert first["商品名"] == "アセロラ[SM2+] SR 056/049"
    assert first["グレード"] == "PSA10"
    assert first["買取価格"] == "1500000"
    assert first["在庫"] == "残り2点"
    assert first["受付状態"] == "募集中"
    assert first["画像URL"] == "https://store.torecabank.com/uploads/product_master/1769854765_1c8df7dfc5bc65a0352c.jpg"

    # closed クラス + "受付終了" は受付終了
    closed = items[1]
    assert closed["受付状態"] == "受付終了"
    assert closed["買取価格"] == "1490000"

    # &amp; が正しくデコードされる
    assert "ゲンガー&ミミッキュ" in items[3]["商品名"]


def test_expected_count():
    assert scrape.expected_count(load_fixture()) == 494


def test_clean_url():
    assert scrape.clean_url("https://x.com//a//b.jpg") == "https://x.com/a/b.jpg"


if __name__ == "__main__":
    test_parse_items()
    test_expected_count()
    test_clean_url()
    print("ALL TESTS PASSED")

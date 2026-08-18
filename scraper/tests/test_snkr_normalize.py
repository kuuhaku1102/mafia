"""スニダン相場スクレイパーの名寄せ(正規化)テスト。

既存 test_parse.py と同じく Playwright も gspread も不要な単体テスト。
snkr_scrape.py は playwright / gspread / bs4 を関数の中でしか import しないため、
このテストは標準ライブラリだけで動く。
"""
import importlib.util
import os

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "snkr_scrape", os.path.join(HERE, "..", "snkr_scrape.py")
)
snkr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snkr)


def test_code_prolonged_sound_mark():
    """211/SMｰP (U+FF70) と 211/SM-P が同一視される。"""
    # 実データの揺れ: ハイフンが半角カナ長音記号 U+FF70 になっている
    assert "ｰ" in "211/SMｰP"
    assert snkr.normalize_code("211/SMｰP") == "211/SM-P"
    assert snkr.normalize_code("211/SM-P") == "211/SM-P"
    assert snkr.normalize_code("211/SMｰP") == snkr.normalize_code("211/SM-P")

    # 全角長音「ー」(U+30FC) 経由でも同じ
    assert snkr.normalize_code("282/SMーP") == "282/SM-P"
    # 全角ハイフン・EN DASH なども吸収
    assert snkr.normalize_code("224/SM－P") == "224/SM-P"
    assert snkr.normalize_code("224/SM–P") == "224/SM-P"


def test_code_lowercase_and_trailing_space():
    """末尾小文字 / 末尾スペースの吸収。"""
    assert snkr.normalize_code("296/XY-p") == "296/XY-P"
    assert snkr.normalize_code("103/S-p") == "103/S-P"
    assert snkr.normalize_code("206/165 ") == "206/165"
    assert snkr.normalize_code(" 206/165　") == "206/165"
    assert snkr.normalize_code("") == ""
    assert snkr.normalize_code(None) == ""


def test_name_prolonged_sound_mark_not_broken():
    """★回帰テスト: 名前の長音記号を '-' にしてはいけない。

    品番用のハイフンセット(長音記号入り)を名前に適用すると語が壊れる:
        ルイージピカチュウ -> ルイ-ジピカチュウ
        ブラッキーVMax     -> ブラッキ-VMax
        リーリエの決心     -> リ-リエの決心
    """
    assert snkr.normalize_name("ルイージピカチュウ") == "ルイージピカチュウ"
    assert snkr.normalize_name("ブラッキーVMax") == "ブラッキーVMax"
    assert snkr.normalize_name("リーリエの決心") == "リーリエの決心"
    for broken in ("ルイ-ジ", "ブラッキ-", "リ-リエ"):
        assert broken not in snkr.normalize_name("ルイージピカチュウ ブラッキーVMax リーリエの決心")

    # 半角カナ長音(U+FF70)が名前に混ざっていても、長音のまま残る (- にはしない)
    assert snkr.normalize_name("ﾙｲｰｼﾞ") == "ルイージ"

    # 一方で、通常のハイフン類は名前でも '-' に統一される
    assert snkr.normalize_name("ピカチュウ–ex") == "ピカチュウ-ex"


def test_name_fullwidth_space_and_newline():
    """全角英字・全角スペース連続・セル内改行の吸収。"""
    assert snkr.normalize_name("ガブリアス＆ギラティナＧＸ") == "ガブリアス&ギラティナGX"
    assert (
        snkr.normalize_name("ポンチョを着たピカチュウ　　(スペシャル)")
        == "ポンチョを着たピカチュウ (スペシャル)"
    )
    assert snkr.normalize_name("オリジンパルキアV(SA) \n\n") == "オリジンパルキアV(SA)"
    assert snkr.normalize_name("") == ""


def test_store_memo_removed_from_search_key():
    """（高い方） などの自店メモが検索キーから除去される。"""
    assert snkr.strip_store_memo("ブラッキーVMax（高い方）") == "ブラッキーVMax"
    assert snkr.strip_store_memo("ブラッキーVMAX 安い方") == "ブラッキーVMAX"

    key = snkr.build_search_key("082/069", "ブラッキーVMax（高い方）")
    assert "高い方" not in key
    assert "（" not in key and "(" not in key
    assert key == "082/069 ブラッキーVMax"

    # カードの正式表記 (SA) は自店メモではないので残す
    assert snkr.build_search_key("", "オリジンパルキアV(SA)") == "オリジンパルキアV(SA)"


def test_search_key_no_duplicate_code():
    """名前に品番が既に含まれる行で、品番が重複しない。"""
    assert (
        snkr.build_search_key("056/049", "アセロラ[SM2+] SR 056/049")
        == "アセロラ[SM2+] SR 056/049"
    )
    # 名前側の品番が U+FF70 表記でも「品番が含まれている」と判定できる
    # (名前側は NFKC で U+FF70 -> U+30FC になるだけで、'-' には変えない)
    assert snkr.build_search_key("211/SM-P", "ピカチュウ 211/SMｰP") == "ピカチュウ 211/SMーP"
    # 含まれない場合は品番を前置
    assert snkr.build_search_key("211/SM-P", "ピカチュウ") == "211/SM-P ピカチュウ"


def test_search_key_empty_sides():
    """名前が空 / 品番が空でも壊れない (例: '206/165 ' で名前が空)。"""
    assert snkr.build_search_key("206/165 ", "") == "206/165"
    assert snkr.build_search_key("", "ピカチュウ") == "ピカチュウ"
    assert snkr.build_search_key("", "") == ""


def test_find_duplicate_codes():
    """品番重複 (データ側の誤り) を検出して人間に返せること。"""
    codes = ["060/054", "060/054", "055/050", "055/050", "211/SMｰP", "211/SM-P", "001/100"]
    dup = snkr.find_duplicate_codes(codes)
    assert dup == {"060/054", "055/050", "211/SM-P"}
    assert snkr.find_duplicate_codes(["", "", "001/100"]) == set()


def test_parse_number():
    assert snkr.parse_number("¥12,800") == "12800"
    assert snkr.parse_number("￥1,234,567 (税込)") == "1234567"
    assert snkr.parse_number("3枚の出品") == "3"
    assert snkr.parse_number("") == ""
    assert snkr.parse_number("在庫なし") == ""


def test_notes():
    assert snkr._add_note("", "検索ヒットなし") == "検索ヒットなし"
    assert snkr._add_note("検索ヒットなし", "検索ヒットなし") == "検索ヒットなし"
    assert snkr._add_note("要確認", "検索ヒットなし") == "要確認 / 検索ヒットなし"
    assert snkr._drop_note("要確認 / 検索ヒットなし", "検索ヒットなし") == "要確認"


def test_hyphen_sets_are_separated():
    """品番用と名前用のハイフン文字セットが分かれていること (仕様の要)。"""
    assert "ｰ" in snkr.CODE_HYPHENS  # 半角カナ長音
    assert "ー" in snkr.CODE_HYPHENS  # 全角カナ長音
    assert "ｰ" not in snkr.COMMON_HYPHENS
    assert "ー" not in snkr.COMMON_HYPHENS


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("ALL TESTS PASSED")

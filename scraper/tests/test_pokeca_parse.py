"""みんなのポケカ相場スクレイパーの単体テスト。

既存 test_parse.py / test_snkr_normalize.py と同じく、
Playwright も gspread も不要 (pokeca_scrape.py はそれらを関数の中でしか
import しないため、標準ライブラリだけで動く)。
"""
import importlib.util
import os

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "pokeca_scrape", os.path.join(HERE, "..", "pokeca_scrape.py")
)
pkc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pkc)


# ---------------------------------------------------------------------------
# 数値・日付のパース
# ---------------------------------------------------------------------------
def test_parse_price():
    assert pkc.parse_price("152,443円") == 152443
    assert pkc.parse_price("¥152,443") == 152443
    assert pkc.parse_price("１５２，４４３") == 152443  # 全角
    assert pkc.parse_price(152443) == 152443
    # 欠損と 0 は必ず区別する (0件と未入力は意味が違う)
    assert pkc.parse_price("") is None
    assert pkc.parse_price("-") is None
    assert pkc.parse_price(None) is None
    assert pkc.parse_price("0") == 0


def test_parse_diff():
    assert pkc.parse_diff("+1,138円(+0.75%)") == (1138, 0.75)
    assert pkc.parse_diff("-1,138円(-0.75%)") == (-1138, -0.75)
    # ▲△ は日本語表記で負を表す慣習
    assert pkc.parse_diff("▲1,138円(▲0.75%)") == (-1138, -0.75)
    assert pkc.parse_diff("±0円(0.00%)") == (0, 0.0)
    assert pkc.parse_diff("") == (None, None)
    assert pkc.parse_diff(None) == (None, None)


def test_parse_date():
    assert pkc.parse_date("2026/08/18") == "2026-08-18"
    assert pkc.parse_date("2026-08-18") == "2026-08-18"
    assert pkc.parse_date("2026年8月18日") == "2026-08-18"
    assert pkc.parse_date("8月18日", default_year=2026) == "2026-08-18"
    assert pkc.parse_date("8月18日") == ""  # 年が無いと決められない
    assert pkc.parse_date("") == ""
    assert pkc.parse_date("2026/13/45") == ""  # 存在しない日付


def test_slug_and_hinban():
    assert pkc.slug_from_url("https://pokeca-chart.com/card/abc-123/") == "abc-123"
    assert pkc.slug_from_url("/card/xyz-9") == "xyz-9"
    assert pkc.slug_from_url("") == ""
    assert pkc.extract_hinban("リザードンex 006/165") == "006/165"
    assert pkc.extract_hinban("名前のみ") == ""


# ---------------------------------------------------------------------------
# upsert (追記型。同じデータを2回入れても重複しないこと)
# ---------------------------------------------------------------------------
def test_record_key_uniqueness():
    """upsert キーが (date, index_type) / (date, card_id, condition) で一意になる。"""
    a = {"date": "2026-08-18", "index_type": "bihin", "value": 100}
    b = {"date": "2026-08-18", "index_type": "psa10", "value": 200}
    assert pkc.record_key(a, pkc.INDEX_KEY_FIELDS) != pkc.record_key(b, pkc.INDEX_KEY_FIELDS)

    c1 = {"date": "2026-08-18", "card_id": "x", "condition": "美品"}
    c2 = {"date": "2026-08-18", "card_id": "x", "condition": "PSA10"}
    c3 = {"date": "2026-08-18", "card_id": "y", "condition": "美品"}
    keys = {pkc.record_key(c, pkc.CARD_KEY_FIELDS) for c in (c1, c2, c3)}
    assert len(keys) == 3
    # 同じ内容なら同じキー
    assert pkc.record_key(c1, pkc.CARD_KEY_FIELDS) == pkc.record_key(dict(c1), pkc.CARD_KEY_FIELDS)


def test_upsert_no_duplicate_on_reinsert():
    """★同じデータを2回投入しても行が重複しない (1日2回走らせても安全)。"""
    headers = pkc.INDEX_HEADERS
    recs = [
        {"date": "2026-08-18", "index_type": "bihin", "value": "100", "fetched_at": "t1"},
        {"date": "2026-08-18", "index_type": "psa10", "value": "200", "fetched_at": "t1"},
    ]
    # 1回目: 空のシートへ
    header, appends, updates, stats = pkc.plan_upsert([], recs, headers, pkc.INDEX_KEY_FIELDS)
    assert stats["appended"] == 2
    assert stats["updated"] == 0

    grid = [header] + appends
    # 2回目: まったく同じデータ -> 追加も更新も発生しない
    _, appends2, updates2, stats2 = pkc.plan_upsert(grid, recs, headers, pkc.INDEX_KEY_FIELDS)
    assert stats2["appended"] == 0, stats2
    assert stats2["updated"] == 0, stats2

    # 3回目: 値だけ変わった -> 追加ではなく更新になる
    recs3 = [dict(recs[0], value="111", fetched_at="t2")]
    _, appends3, updates3, stats3 = pkc.plan_upsert(grid, recs3, headers, pkc.INDEX_KEY_FIELDS)
    assert stats3["appended"] == 0
    assert stats3["updated"] == 1
    assert updates3[0][0] == 2  # ヘッダーが1行目なのでデータは2行目から


def test_upsert_dedupes_within_one_batch():
    """同じ実行内に同じキーが2回来ても1行にまとまる。"""
    headers = pkc.INDEX_HEADERS
    recs = [
        {"date": "2026-08-18", "index_type": "bihin", "value": "100"},
        {"date": "2026-08-18", "index_type": "bihin", "value": "150"},  # 後勝ち
    ]
    header, appends, _, stats = pkc.plan_upsert([], recs, headers, pkc.INDEX_KEY_FIELDS)
    assert stats["appended"] == 1
    assert appends[0][header.index("value")] == "150"


def test_upsert_preserves_extra_columns():
    """人間が足した列を消さない (手入力が飛ぶのを防ぐ)。"""
    headers = pkc.INDEX_HEADERS
    header0 = pkc.INDEX_HEADERS + ["手入力メモ"]
    grid = [header0, ["2026-08-18", "bihin", "100", "", "", "t1", "残しておきたい"]]
    recs = [{"date": "2026-08-18", "index_type": "bihin", "value": "111", "fetched_at": "t2"}]
    header, appends, updates, stats = pkc.plan_upsert(grid, recs, headers, pkc.INDEX_KEY_FIELDS)
    assert stats["updated"] == 1
    updated_row = updates[0][1]
    assert updated_row[header.index("手入力メモ")] == "残しておきたい"
    assert updated_row[header.index("value")] == "111"


# ---------------------------------------------------------------------------
# ★補完日の検出と除外 (このスクレイパーの成否を分ける部分)
# ---------------------------------------------------------------------------
def test_imputed_flag_from_trade_count():
    """取引件数が 0 / 欠損 の日に補完疑いフラグが立つ。"""
    series = [
        {"date": "2026-08-01", "price": 100, "trade_count": 5},
        {"date": "2026-08-02", "price": 100, "trade_count": 0},     # 取引なし = 補完
        {"date": "2026-08-03", "price": 100, "trade_count": None},  # 欠損 = 実測ではない
        {"date": "2026-08-04", "price": 110, "trade_count": 3},
    ]
    rows, basis = pkc.mark_imputed(series)
    assert basis == "trade_count"
    assert [r["imputed_suspect"] for r in rows] == [False, True, True, False]


def test_imputed_flag_fallback_price_repeat():
    """取引件数が取れない場合は、前日と価格が完全一致した日を補完候補にする。"""
    series = [
        {"date": "2026-08-01", "price": 100, "trade_count": None},
        {"date": "2026-08-02", "price": 100, "trade_count": None},  # 前日と同値 = 補完候補
        {"date": "2026-08-03", "price": 105, "trade_count": None},
    ]
    rows, basis = pkc.mark_imputed(series)
    assert basis == "price_repeat"
    assert [r["imputed_suspect"] for r in rows] == [False, True, False]


def test_imputed_days_excluded_from_ma_and_streak():
    """★4-B章の罠の回帰テスト。

    「5日間まったく取引が無かった薄い銘柄」を
    「5日連続で価格が安定している優良銘柄」と誤認しないこと。
    補完日は平均にも streak にも入れない (前方補完もしない)。
    """
    # 下落したあと、取引が無い日が5日続いた系列
    series = [
        {"date": "2026-08-01", "price": 120, "trade_count": 4},
        {"date": "2026-08-02", "price": 115, "trade_count": 3},
        {"date": "2026-08-03", "price": 110, "trade_count": 2},
        # ここから5日間まったく取引が無く、前日価格 110 が引き継がれている
        {"date": "2026-08-04", "price": 110, "trade_count": 0},
        {"date": "2026-08-05", "price": 110, "trade_count": 0},
        {"date": "2026-08-06", "price": 110, "trade_count": 0},
        {"date": "2026-08-07", "price": 110, "trade_count": 0},
        {"date": "2026-08-08", "price": 110, "trade_count": 0},
    ]
    m = pkc.compute_metrics(series)

    # 有効観測日は3日だけ。8日ぶんの「安定」ではない。
    assert m["valid_days"] == 3, m
    # 直近の変化は 115->110 の下落であって、0%(横ばい)ではない
    assert m["d1"] is not None and m["d1"] < 0, m
    # streak は下落2連続。補完日を含めていたら 0 や正の値になってしまう
    assert m["streak"] == -2, m
    # ma5 は有効日が5日に満たないので計算しない (110 の平坦な線を作らない)
    assert m["ma5"] is None, m

    # 補完日を除外しなかった場合との対比: 除外しないと streak が壊れる
    naive = [dict(r, trade_count=1) for r in series]
    m_naive = pkc.compute_metrics(naive)
    assert m_naive["valid_days"] == 8
    assert m_naive["streak"] == 0  # 同値が続くので方向が消える = 下落を見失う


# ---------------------------------------------------------------------------
# 傾向判定
# ---------------------------------------------------------------------------
def _series(prices, trade_count=3, start_day=1):
    return [
        {"date": f"2026-08-{start_day + i:02d}", "price": p, "trade_count": trade_count}
        for i, p in enumerate(prices)
    ]


def test_trend_undecidable_when_few_valid_days():
    """有効観測日が MIN_VALID_DAYS 未満の銘柄は判定不可 (無理に判定しない)。"""
    m = pkc.compute_metrics(_series([100, 99, 98, 97, 96]))
    assert m["valid_days"] == 5
    assert pkc.classify_trend(m) == "判定不可"

    # 20日ぶんあっても、取引があったのが数日だけなら判定不可
    rows = _series([100 - i for i in range(20)])
    for r in rows[pkc.MIN_VALID_DAYS - 1:]:
        r["trade_count"] = 0
    m2 = pkc.compute_metrics(rows)
    assert m2["valid_days"] == pkc.MIN_VALID_DAYS - 1
    assert pkc.classify_trend(m2) == "判定不可"


def test_one_week_is_enough_to_judge():
    """★直近1週間ぶんの実測があれば方向を判定できる。

    用途が「2〜3日先にどちらへ動くか」なので、判定開始を1週間にしてある。
    """
    assert pkc.MIN_VALID_DAYS == 7

    # 有効観測7日ちょうどの下落系列
    m = pkc.compute_metrics(_series([200 - i * 3 for i in range(7)]))
    assert m["valid_days"] == 7
    assert pkc.classify_trend(m) == "下落", m
    # 7日ちょうどでも d7 が出ること (7観測ぶんの幅として計算する)
    assert m["d7"] is not None and m["d7"] < 0
    assert float(pkc.suggested_buffer_pct(m, "下落")) > 0

    # 6日では判定不可のまま
    m6 = pkc.compute_metrics(_series([200 - i * 3 for i in range(6)]))
    assert m6["valid_days"] == 6
    assert pkc.classify_trend(m6) == "判定不可"
    assert m6["d7"] is None


def test_d7_uses_valid_observations_only():
    """d7 は「有効観測7日ぶん」の幅。補完日は数に入れない。

    薄い銘柄では実時間で2週間以上をまたぐことがある (それが正しい)。
    """
    # 14日ぶんあるが、取引があったのは1日おきの7日だけ
    rows = _series([100 - i for i in range(14)])
    for i, r in enumerate(rows):
        if i % 2 == 1:
            r["trade_count"] = 0
    m = pkc.compute_metrics(rows)
    assert m["valid_days"] == 7
    # 有効な7点は 100, 98, 96, 94, 92, 90, 88 -> (88/100 - 1) * 100
    assert abs(m["d7"] - (-12.0)) < 0.01, m["d7"]


def test_trend_down():
    """はっきり下げている系列は 下落 になる。"""
    prices = [200 - i * 3 for i in range(20)]  # 単調減少
    m = pkc.compute_metrics(_series(prices))
    assert m["valid_days"] == 20
    assert m["d3"] < 0 and m["ma5"] < m["ma20"] and m["streak"] <= -2
    assert pkc.classify_trend(m) == "下落"
    # 下落時はバッファの目安が出る (買取率の調整に使う)
    buf = pkc.suggested_buffer_pct(m, "下落")
    assert buf and float(buf) > 0


def test_trend_up():
    """はっきり上げている系列は 上昇 になる。"""
    prices = [100 + i * 3 for i in range(20)]
    m = pkc.compute_metrics(_series(prices))
    assert m["d3"] > 0 and m["ma5"] > m["ma20"] and m["streak"] >= 3
    assert pkc.classify_trend(m) == "上昇"
    # 上昇時にバッファは出さない
    assert pkc.suggested_buffer_pct(m, "上昇") == ""


def test_trend_flat():
    """小さく上下しているだけの系列は 横ばい。"""
    prices = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101,
              100, 101, 100, 101, 100, 101, 100, 101, 100, 101]
    m = pkc.compute_metrics(_series(prices))
    assert m["valid_days"] == 20
    assert pkc.classify_trend(m) == "横ばい"


def test_thresholds_are_asymmetric():
    """★下落側を緩く、上昇側を厳しく (損を避けるための非対称)。"""
    assert abs(pkc.DOWN_D3) < abs(pkc.UP_D3), (pkc.DOWN_D3, pkc.UP_D3)
    assert abs(pkc.DOWN_STREAK) < abs(pkc.UP_STREAK), (pkc.DOWN_STREAK, pkc.UP_STREAK)

    # 同じ大きさの動きなら、下落側の方が先にフラグが立つ
    down = pkc.compute_metrics(_series([100 - i * 0.5 for i in range(20)]))
    up = pkc.compute_metrics(_series([100 + i * 0.5 for i in range(20)]))
    assert pkc.classify_trend(down) == "下落"
    assert pkc.classify_trend(up) == "横ばい", "同じ傾きの上昇は下落より判定を厳しくする"


def test_build_trend_row_shape():
    """出力行に valid_days と判定根拠が必ず入る (人間が信頼度を見るため)。"""
    row = pkc.build_trend_row("card", "abc|美品", _series([200 - i * 3 for i in range(20)]))
    for h in pkc.TREND_HEADERS:
        assert h in row, h
    assert row["trend"] == "下落"
    assert row["valid_days"] == "20"
    assert row["imputation_basis"] == "trade_count"
    assert row["scope"] == "card"


def test_group_series_excludes_imputed():
    """グループ集約でも補完日は混ぜない。"""
    rows = [
        {"date": "2026-08-01", "price": 100, "trade_count": 2, "condition": "美品"},
        {"date": "2026-08-01", "price": 200, "trade_count": 3, "condition": "美品"},
        {"date": "2026-08-02", "price": 100, "trade_count": 0, "condition": "美品"},  # 補完
        {"date": "2026-08-02", "price": 300, "trade_count": 1, "condition": "美品"},
    ]
    g = pkc.group_series(rows, group_of=lambda r: r.get("condition") or "")
    assert g["美品"][0]["price"] == 150  # (100+200)/2
    assert g["美品"][1]["price"] == 300  # 補完の 100 を混ぜない
    assert g["美品"][0]["trade_count"] == 5


# ---------------------------------------------------------------------------
# JSON からの系列抽出 (キー名が未確認でも拾えること)
# ---------------------------------------------------------------------------
def test_normalize_and_walk_series():
    payload = {
        "props": {
            "chart": [
                {"date": "2026-08-01", "price": 100, "tradeCount": 3},
                {"date": "2026-08-02", "price": 110, "tradeCount": 0},
            ]
        }
    }
    found = pkc.walk_series(payload)
    assert len(found) == 1
    path, series = found[0]
    assert "chart" in path
    assert series[0] == {"date": "2026-08-01", "price": 100, "trade_count": 3}
    assert pkc.has_trade_count(series) is True

    # 取引件数が無い系列は has_trade_count が False
    no_count = pkc.normalize_series([{"date": "2026-08-01", "value": 100}])
    assert no_count[0]["trade_count"] is None
    assert pkc.has_trade_count(no_count) is False


def test_find_json_blobs_next_data():
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"series":[{"d":"2026-08-01","y":100},{"d":"2026-08-02","y":120}]}}'
        "</script></html>"
    )
    blobs = pkc.find_json_blobs(html)
    assert blobs
    found = pkc.walk_series(blobs[0])
    assert found and len(found[0][1]) == 2


def test_iter_json_values():
    """RSCペイロードのように地の文へ混ざったJSONも拾える。"""
    text = 'a:b:[{"date":"2026-08-01","price":100},{"date":"2026-08-02","price":110}] tail'
    vals = list(pkc.iter_json_values(text))
    assert any(isinstance(v, list) and len(v) == 2 for v in vals)


# ---------------------------------------------------------------------------
# 対象カードの指定・照会 (品番/名前の表記ゆれを吸収して引けること)
# ---------------------------------------------------------------------------
def test_normalize_hinban_and_name():
    """★ハイフン文字セットを品番用と名前用で分ける。

    品番の U+FF70 は '-' に寄せる必要があるが、
    同じ変換を名前に当てると長音が壊れる。
    """
    assert pkc.normalize_hinban("006/165 ") == "006/165"
    assert pkc.normalize_hinban("211/SMｰP") == "211/SM-P"   # 半角カナ長音
    assert pkc.normalize_hinban("211/SMーP") == "211/SM-P"   # 全角カナ長音
    assert pkc.normalize_hinban("296/XY-p") == "296/XY-P"

    # 名前の長音は絶対に壊さない
    assert pkc.normalize_name("ルイージピカチュウ") == "ルイージピカチュウ"
    assert pkc.normalize_name("ブラッキーVMax") == "ブラッキーVMax"
    assert pkc.normalize_name("リーリエの決心") == "リーリエの決心"
    # 全角英字・全角スペース・セル内改行は吸収する
    assert pkc.normalize_name("ガブリアス＆ギラティナＧＸ") == "ガブリアス&ギラティナGX"
    assert pkc.normalize_name("リザードン\n\n") == "リザードン"


def test_match_cards():
    """品番 / 名前 / card_id のどれでもカードを引ける。"""
    cards = [
        {"card_id": "rizadon-006", "card_name": "リザードンex", "hinban": "006/165"},
        {"card_id": "pikachu-211", "card_name": "ルイージピカチュウ", "hinban": "211/SMｰP"},
        {"card_id": "blacky-082", "card_name": "ブラッキーVMax", "hinban": "082/069"},
    ]
    # 品番で引く (表記ゆれを吸収)
    assert pkc.match_cards("006/165", cards)[0]["card_id"] == "rizadon-006"
    assert pkc.match_cards("211/SM-P", cards)[0]["card_id"] == "pikachu-211"
    assert pkc.match_cards("211/SMｰP", cards)[0]["card_id"] == "pikachu-211"
    # 名前で引く (完全一致・部分一致)
    assert pkc.match_cards("ブラッキーVMax", cards)[0]["card_id"] == "blacky-082"
    assert pkc.match_cards("リザードン", cards)[0]["card_id"] == "rizadon-006"
    # card_id で引く
    assert pkc.match_cards("blacky-082", cards)[0]["card_id"] == "blacky-082"
    # URL で引く
    assert pkc.match_cards(
        "https://pokeca-chart.com/card/blacky-082/", cards
    )[0]["card_id"] == "blacky-082"
    # 見つからないものは空
    assert pkc.match_cards("存在しないカード", cards) == []
    assert pkc.match_cards("", cards) == []


def test_match_cards_ranking():
    """完全一致が部分一致より上に来る。"""
    cards = [
        {"card_id": "a", "card_name": "リザードンex SAR", "hinban": "201/165"},
        {"card_id": "b", "card_name": "リザードン", "hinban": "006/165"},
    ]
    hits = pkc.match_cards("リザードン", cards)
    assert hits[0]["card_id"] == "b", [h["card_id"] for h in hits]
    assert len(hits) == 2  # 候補は絞らず全部返して人間に選ばせる


# ---------------------------------------------------------------------------
# ★ウォッチリストの照合は「品番+名前」で確定させる
# ---------------------------------------------------------------------------
# 品番は弾をまたぐと重複する ("006/165" は複数のセットに存在しうる)。
# 品番だけで先頭を機械的に採用すると、別のカードを掴む。
MASTER = [
    {"card_id": "sv2a-006", "card_name": "リザードンex", "hinban": "006/165"},
    {"card_id": "old-006", "card_name": "ヤミラミ", "hinban": "006/165"},   # 品番が重複
    {"card_id": "luigi-211", "card_name": "ルイージピカチュウ", "hinban": "211/SMｰP"},
]


def test_watchlist_hinban_plus_name_resolves():
    """品番+名前が両方合えば一意に確定する。"""
    best, cands, reason = pkc.match_watchlist_row(
        {"品番": "006/165", "名前": "リザードンex"}, MASTER)
    assert best["card_id"] == "sv2a-006"
    assert reason == ""

    # 同じ品番でも名前が違えば別のカードを引く
    best2, _, _ = pkc.match_watchlist_row({"品番": "006/165", "名前": "ヤミラミ"}, MASTER)
    assert best2["card_id"] == "old-006"


def test_watchlist_hinban_only_is_ambiguous():
    """★品番だけで候補が複数なら確定させない（別カードを掴む事故を防ぐ）。"""
    best, cands, reason = pkc.match_watchlist_row({"品番": "006/165"}, MASTER)
    assert best is None, "品番だけで先頭を勝手に採用してはいけない"
    assert len(cands) == 2
    assert "名前も入力" in reason

    # 品番だけでも候補が1件なら確定してよい
    best2, _, reason2 = pkc.match_watchlist_row({"品番": "211/SM-P"}, MASTER)
    assert best2["card_id"] == "luigi-211"
    assert reason2 == ""


def test_watchlist_name_mismatch_does_not_fall_back():
    """★品番は合うが名前が合わない場合、品番だけの候補で代用しない。"""
    best, cands, reason = pkc.match_watchlist_row(
        {"品番": "006/165", "名前": "存在しないカード"}, MASTER)
    assert best is None
    assert "名前が合いません" in reason
    assert len(cands) == 2  # 候補は人間に見せる


def test_watchlist_normalizes_hinban_variants():
    """表記ゆれの品番でも同じカードに当たる。"""
    for h in ("211/SMｰP", "211/SM-P", "211/SMーP", "211/sm-p ", "２１１／ＳＭ－Ｐ"):
        best, _, _ = pkc.match_watchlist_row({"品番": h, "名前": "ルイージピカチュウ"}, MASTER)
        assert best and best["card_id"] == "luigi-211", h


def test_watchlist_row_key_is_hinban_plus_name():
    """upsert キーが品番+名前なので、同じ銘柄が2行に増えない。"""
    assert pkc.WATCHLIST_KEY_FIELDS == ["品番", "名前"]
    a = {"品番": "006/165", "名前": "リザードンex"}
    b = {"品番": "006/165", "名前": "ヤミラミ"}
    assert pkc.record_key(a, pkc.WATCHLIST_KEY_FIELDS) != pkc.record_key(b, pkc.WATCHLIST_KEY_FIELDS)


def test_watchlist_empty_row_reports_reason():
    """空行は理由を返す（黙って無視しない）。"""
    best, _, reason = pkc.match_watchlist_row({"品番": "", "名前": ""}, MASTER)
    assert best is None
    assert "入力してください" in reason


# ---------------------------------------------------------------------------
# ★買取表の商品名を扱う（実データに基づく）
# ---------------------------------------------------------------------------
def test_split_product_name():
    """商品名から品番を切り出す（買取表からのコピペ想定）。"""
    cases = [
        ("リーリエ(エクストラバトルの日) PROMO 397/SM-P", "397/SM-P"),
        ("ルイージピカチュウ(大) PROMO 296/XY-P", "296/XY-P"),
        ("アセロラ[SM2+] SR 056/049", "056/049"),
        ("ゲンガーEX:1ED[XY4] SR 090/088", "090/088"),
        ("ピカチュウ(マクドナルド) PROMO 020/M-P", "020/M-P"),
        ("キャプテンピカチュウ(中国語版)[CBB1C] AR 0709/09", "0709/09"),
        ("ピカチュウV[SI] - 415/414", "415/414"),
        ("リザードン LV.76[OP1] ★ No.006", "No.006"),   # 旧弾は No. 形式
    ]
    for product, expected in cases:
        hinban, name = pkc.split_product_name(product)
        assert hinban == expected, (product, hinban)
        assert name and expected not in name, (product, name)

    # 品番が無い商品名でも壊れない
    h, n = pkc.split_product_name("ミュウツーEX(20th アニバーサリーフェスタ) PROMO XY-P")
    assert h == ""
    assert "ミュウツーEX" in n
    assert pkc.split_product_name("") == ("", "")


def test_classify_product_rejects_other_tcg():
    """★pokeca-chart.com はポケカ専門。他TCGは「対象外」として区別する。

    照合できないのは入力の誤りではないので、エラー扱いにしない。
    """
    assert pkc.classify_product("アセロラ[SM2+] SR 056/049") == "ポケカ"
    assert pkc.classify_product("リザードンex[SV2a] SAR 201/165") == "ポケカ"

    assert pkc.classify_product("孫悟空 SCR☆☆ FB05-119") == "ドラゴンボールFW"
    assert pkc.classify_product("ベジータ SR☆ FB02-133") == "ドラゴンボールFW"
    assert pkc.classify_product("海辺の街でキミと AZKi SSP HOL/W104-082SSP") == "ヴァイスシュヴァルツ"
    assert pkc.classify_product("まどろみのひと時 エミリア SP RZ/S46-T42SP") == "ヴァイスシュヴァルツ"
    assert pkc.classify_product("蒼翠の風霊使いウィン QCCU-JP188") == "遊戯王"
    assert pkc.classify_product("バギー(illust:otton) L OP09-042") == "ワンピースカード"
    assert pkc.classify_product("【未開封BOX】MANGA BOOSTER 01") == "未開封BOX等"
    assert pkc.classify_product("拡張パック『フュージョンアーツ』(S8)【未開封BOX】") == "未開封BOX等"
    assert pkc.classify_product("エナジーマーカー ☆ E-48") == "エナジーマーカー"

    # ヴァイスの品番はポケカの品番として誤検出されないこと
    h, _ = pkc.split_product_name("海辺の街でキミと AZKi SSP HOL/W104-082SSP")
    assert h == "", h


def test_core_name_strips_set_and_rarity():
    """買取表の名前から、比較用の核となる名前を作る。"""
    assert pkc.core_name("アセロラ[SM2+] SR") == "アセロラ"
    assert pkc.core_name("リザードンex[SV2a] SAR") == "リザードンex".lower()
    assert pkc.core_name("ピカチュウ&ゼクロムGX[SM9] SR(SA)") == "ピカチュウ&ゼクロムgx"
    # 括弧の補足は意味を持つので残す
    assert "(大)" in pkc.core_name("ルイージピカチュウ(大) PROMO")


def test_names_match_is_bidirectional():
    """★包含は双方向。買取表側が長いこともマスタ側が長いこともある。"""
    # 買取表の方が長い（実データで実際に外れていたケース）
    assert pkc.names_match("アセロラ[SM2+] SR", "アセロラ")
    assert pkc.names_match("リザードンex[SV2a] SAR", "リザードンex")
    # マスタの方が長い
    assert pkc.names_match("リーリエ", "リーリエ(エクストラバトルの日)")
    # 別カードは一致しない
    assert not pkc.names_match("アセロラ[SM2+] SR", "マオ")
    assert not pkc.names_match("", "アセロラ")


def test_watchlist_accepts_product_name_column():
    """商品名を1列貼るだけで、品番+名前に分解して照合できる。"""
    master = [{"card_id": "ase-056", "card_name": "アセロラ",
               "hinban": "056/049", "url": "u"}]
    watch = [
        {"商品名": "アセロラ[SM2+] SR 056/049"},
        {"商品名": "孫悟空 SCR☆☆ FB05-119"},          # 対象外
        {"商品名": "エナジーマーカー ☆ E-48"},            # 対象外
    ]
    out = pkc.resolve_watchlist(None, watch, master)
    assert out[0]["card_id"] == "ase-056"
    assert out[0]["品番"] == "056/049"
    assert "対象外" in out[1]["メモ"] and "ドラゴンボール" in out[1]["メモ"]
    assert "対象外" in out[2]["メモ"]
    # 対象外の行は card_id を埋めない（巡回対象にしない）
    assert not out[1].get("card_id")
    assert "商品名" in pkc.WATCHLIST_HEADERS


def test_resolve_skips_when_master_empty():
    """★マスタが0件なら照合を試みない（205行のエラーを並べない）。"""
    watch = [{"商品名": f"カード{i} 00{i}/165"} for i in range(5)]
    out = pkc.resolve_watchlist(None, watch, [])
    assert len(out) == 5
    assert all(not r.get("メモ") for r in out), "入力を書き換えないこと"


# ---------------------------------------------------------------------------
# ★実際の買取表（品番列 + 名前列）を扱う
# ---------------------------------------------------------------------------
def test_missing_hinban_rows_are_not_excluded():
    """★品番が空でも除外しない（買取表には普通にある）。

    "マオ&スイレン" "ナタネ SR" "アカネ" などは品番が空だが正当なポケカ。
    名前で照合できるので対象から外してはいけない。
    """
    for name in ("マオ&スイレン", "ナタネ SR", "アカネ", "ソニア", "カイ SR",
                 "(PSA10)レシラムex【BWR】"):
        assert pkc.classify_product(f" {name}") == "ポケカ", name

    # 商品名を1列に貼った形式のときだけ品番を必須にする
    assert pkc.classify_product("ポートガス・D・エース P P-074",
                                require_hinban=True) != "ポケカ"

    master = [{"card_id": "mao", "card_name": "マオ&スイレン", "hinban": "", "url": "u"}]
    out = pkc.resolve_watchlist(None, [{"品番": "", "名前": "マオ&スイレン"}], master)
    assert out[0]["card_id"] == "mao"


def test_psa10_marker_becomes_condition():
    """名前の (PSA10) は「鑑定品の相場が見たい」という指定。状態列へ移す。"""
    assert pkc.extract_condition("(PSA10)リザードンV") == ("PSA10", "リザードンV")
    assert pkc.extract_condition("（PSA10）レックウザVMAX")[0] == "PSA10"
    assert pkc.extract_condition("PSA 10 リザードンV")[0] == "PSA10"
    assert pkc.extract_condition("リザードンV") == ("", "リザードンV")

    master = [{"card_id": "rv", "card_name": "リザードンV", "hinban": "211/172", "url": "u"}]
    out = pkc.resolve_watchlist(None, [{"品番": "211/172", "名前": "(PSA10)リザードンV"}], master)
    assert out[0]["状態"] == "PSA10"
    assert out[0]["card_id"] == "rv"
    # 名前列そのものは書き換えない（元の入力を残す）
    assert "PSA10" in out[0]["名前"]
    assert "状態" in pkc.WATCHLIST_HEADERS


def test_store_memo_stripped_for_matching():
    """★自店メモ（高い方 / 安い方）を落とさないとマスタに当たらない。"""
    assert pkc.strip_store_memo("ブラッキーVMax（高い方）") == "ブラッキーVMax"
    assert pkc.strip_store_memo("ニンフィアVMAX 安い方") == "ニンフィアVMAX"
    assert pkc.names_match("ブラッキーVMax　（高い方）", "ブラッキーVMAX")
    assert pkc.names_match("ニンフィアVMAX 　安い方", "ニンフィアVMAX")
    # 別カードには当たらない
    assert not pkc.names_match("ブラッキーVMax（高い方）", "ニンフィアVMAX")


def test_real_watchlist_hinban_variants():
    """実データの品番の揺れを吸収する。"""
    cases = {
        "211/SMｰP": "211/SM-P",   # 半角カナ長音
        "282/SMｰP": "282/SM-P",
        "189/SｰP": "189/S-P",
        "296/XY-p": "296/XY-P",   # 末尾小文字
        "103/S-p": "103/S-P",
        "206/165 ": "206/165",    # 末尾スペース
    }
    for raw, expected in cases.items():
        assert pkc.normalize_hinban(raw) == expected, raw


def test_real_watchlist_name_variants():
    """全角英字・全角スペース連続・セル内改行を吸収する。"""
    assert pkc.normalize_name("ガブリアス＆ギラティナＧＸ　ＳＲ") == "ガブリアス&ギラティナGX SR"
    assert pkc.normalize_name("ポンチョを着たピカチュウ　　（オレンジリザードン）") == \
        "ポンチョを着たピカチュウ (オレンジリザードン)"
    assert pkc.normalize_name("オリジンパルキアV(SA) \n\n") == "オリジンパルキアV(SA)"
    # ★長音は壊さない（品番用の変換を名前に当ててはいけない）
    for n in ("ルイージピカチュウ（大）", "ブラッキーVMax", "リーリエ"):
        assert "-" not in pkc.normalize_name(n).replace("VMax", ""), n


def test_duplicate_hinban_flagged():
    """★品番の重複はデータ側の誤り。勝手に直さず備考で人間に返す。"""
    master = [{"card_id": "x", "card_name": "まったく別のカード", "hinban": "999/999", "url": "u"}]
    watch = [
        {"品番": "060/054", "名前": "ファイヤーサンダーフリーザーGX"},
        {"品番": "060/054", "名前": "ガブリアス＆ギラティナＧＸ　ＳＲ"},
        {"品番": "055/050", "名前": "マオ"},
        {"品番": "055/050", "名前": "ルザミーネ（白）"},
        {"品番": "119/114", "名前": "リーリエ"},
    ]
    out = pkc.resolve_watchlist(None, watch, master)
    flagged = [r for r in out if "品番重複" in (r.get("メモ") or "")]
    assert len(flagged) == 4, [r.get("メモ") for r in out]
    # 重複していない行には印を付けない
    assert "品番重複" not in (out[4].get("メモ") or "")


def test_trend_row_has_readable_label():
    """key は機械的なまま、人間向けの名前は label 列に入れる。

    key を人間向けにすると、カード名が変わった瞬間に別行として増えてしまう。
    """
    row = pkc.build_trend_row(
        "card", "rizadon-006|美品",
        [{"date": f"2026-08-{i+1:02d}", "price": 200 - i * 3, "trade_count": 2}
         for i in range(20)],
        label="リザードンex [006/165] 美品",
    )
    assert row["key"] == "rizadon-006|美品"
    assert row["label"] == "リザードンex [006/165] 美品"
    assert "label" in pkc.TREND_HEADERS
    # label は upsert キーに含めない (名前が変わっても同じ行を更新する)
    assert "label" not in pkc.TREND_KEY_FIELDS


# ---------------------------------------------------------------------------
# 現況ボード (対象カードの最新状態をシートへ転記する)
# ---------------------------------------------------------------------------
def _hist(cid, name, hinban, cond, prices, counts):
    return [
        {"date": f"2026-08-{i + 1:02d}", "card_id": cid, "card_name": name,
         "hinban": hinban, "condition": cond, "price": str(p), "trade_count": str(c)}
        for i, (p, c) in enumerate(zip(prices, counts))
    ]


def test_price_trail_marks_imputed():
    """直近推移のセルで、補完日に * が付く。"""
    marked, _ = pkc.mark_imputed([
        {"date": "2026-08-01", "price": 100, "trade_count": 2},
        {"date": "2026-08-02", "price": 100, "trade_count": 0},   # 補完
        {"date": "2026-08-03", "price": 98, "trade_count": 1},
    ])
    assert pkc.price_trail(marked) == "100→100*→98"
    assert pkc.price_trail([]) == ""


def test_price_trail_is_one_week():
    """直近推移は1週間ぶん (7観測) を1セルに収める。"""
    marked, _ = pkc.mark_imputed([
        {"date": f"2026-08-{i + 1:02d}", "price": 100 - i, "trade_count": 2}
        for i in range(20)
    ])
    trail = pkc.price_trail(marked)
    assert len(trail.split("→")) == 7, trail
    assert trail.endswith("81")  # 最新が末尾


def test_status_has_d7_column():
    """現況ボードに1週間の変化率が載る。"""
    rows = _hist("a", "カードA", "001/100", "美品",
                 [200 - i * 3 for i in range(20)], [2] * 20)
    r = pkc.build_status_rows(rows)[0]
    assert "d7%" in pkc.STATUS_HEADERS
    assert "直近1週間" in pkc.STATUS_HEADERS
    assert float(r["d7%"]) < 0
    assert "d7" in pkc.TREND_HEADERS


def test_build_status_rows():
    """1銘柄1行の現況行が作られる。"""
    rows = _hist("riza-006", "リザードンex", "006/165", "美品",
                 [200 - i * 3 for i in range(20)], [2] * 20)
    status = pkc.build_status_rows(rows)
    assert len(status) == 1
    r = status[0]
    for h in pkc.STATUS_HEADERS:
        assert h in r, h
    assert r["品番"] == "006/165"
    assert r["名前"] == "リザードンex"
    assert r["状態"] == "美品"
    assert r["傾向"] == "下落"
    assert r["card_id"] == "riza-006"
    assert float(r["買取調整%"]) > 0
    assert r["有効観測日"] == 20
    assert "→" in r["直近1週間"]


def test_status_flags_imputed_latest():
    """最新日が補完だった場合に、その価格が実測でないことが見える。"""
    rows = _hist("x", "テスト", "001/100", "美品",
                 [100 - i for i in range(19)] + [82], [2] * 19 + [0])
    r = pkc.build_status_rows(rows)[0]
    assert r["最新が補完"] == "★補完"
    assert r["直近1週間"].endswith("*")


def test_status_only_watchlisted_cards():
    """★品番を入れた銘柄だけをボードに載せる。"""
    rows = (
        _hist("a", "カードA", "001/100", "美品", [100 - i for i in range(20)], [2] * 20)
        + _hist("b", "カードB", "002/100", "美品", [100 - i for i in range(20)], [2] * 20)
    )
    both = pkc.build_status_rows(rows)
    assert {r["card_id"] for r in both} == {"a", "b"}

    only_a = pkc.build_status_rows(rows, only_ids={"a"})
    assert {r["card_id"] for r in only_a} == {"a"}
    assert pkc.build_status_rows(rows, only_ids=set()) == []


def test_status_sorted_by_urgency():
    """注意が要る順 (下落 → 判定不可 → 横ばい → 上昇) に並ぶ。"""
    rows = (
        _hist("up", "上昇", "003/100", "美品", [100 + i * 3 for i in range(20)], [2] * 20)
        + _hist("down", "下落", "001/100", "美品", [200 - i * 3 for i in range(20)], [2] * 20)
        + _hist("few", "判定不可", "002/100", "美品", [100, 99, 98], [2] * 3)
    )
    status = pkc.build_status_rows(rows)
    assert [r["傾向"] for r in status][:2] == ["下落", "判定不可"], [r["傾向"] for r in status]


def test_status_upsert_does_not_grow_rows():
    """★現況ボードは行が増えない (1銘柄1行を上書き更新する view)。

    日付をキーに含めないので、毎日走らせても行数は一定。
    履歴は pkc_card_history / pkc_trend 側が持つ。
    """
    rows = _hist("a", "カードA", "001/100", "美品",
                 [100 - i for i in range(20)], [2] * 20)
    day1 = pkc.build_status_rows(rows)
    header, appends, _, stats = pkc.plan_upsert(
        [], day1, pkc.STATUS_HEADERS, pkc.STATUS_KEY_FIELDS)
    assert stats["appended"] == 1

    grid = [header] + appends
    # 翌日: 価格が1日ぶん増えても行は増えず、既存行が更新される
    rows2 = rows + _hist("a", "カードA", "001/100", "美品", [79], [3])
    rows2[-1]["date"] = "2026-08-21"
    day2 = pkc.build_status_rows(rows2)
    _, appends2, updates2, stats2 = pkc.plan_upsert(
        grid, day2, pkc.STATUS_HEADERS, pkc.STATUS_KEY_FIELDS)
    assert stats2["appended"] == 0, stats2
    assert stats2["updated"] == 1, stats2


def test_status_separates_conditions():
    """同じカードでも状態(美品/PSA10)ごとに別行になる。"""
    rows = (
        _hist("a", "カードA", "001/100", "美品", [100 - i for i in range(20)], [2] * 20)
        + _hist("a", "カードA", "001/100", "PSA10", [500 - i for i in range(20)], [2] * 20)
    )
    status = pkc.build_status_rows(rows)
    assert len(status) == 2
    assert {r["状態"] for r in status} == {"美品", "PSA10"}
    # キーが状態を含むので互いを上書きしない
    keys = {pkc.record_key(r, pkc.STATUS_KEY_FIELDS) for r in status}
    assert len(keys) == 2


def test_request_delay_floor():
    """★相手先への配慮: リクエスト間隔は下限未満に下げられない。"""
    assert pkc.REQUEST_DELAY >= pkc.MIN_REQUEST_DELAY
    assert pkc.MIN_REQUEST_DELAY == 5.0


def test_robots_allows():
    robots = "User-agent: *\nDisallow: /admin/\nAllow: /\n"
    assert pkc.robots_allows(robots, "/") is True
    assert pkc.robots_allows(robots, "/all-card/") is True
    assert pkc.robots_allows(robots, "/admin/secret") is False
    # 取得できなかった場合は判定不能 (禁止と断定しない)
    assert pkc.robots_allows(None, "/") is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")
    print("ALL TESTS PASSED")

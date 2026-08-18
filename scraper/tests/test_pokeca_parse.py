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
    """有効観測日が10日未満の銘柄は判定不可 (無理に判定しない)。"""
    m = pkc.compute_metrics(_series([100, 99, 98, 97, 96]))
    assert m["valid_days"] == 5
    assert pkc.classify_trend(m) == "判定不可"

    # 20日ぶんあっても、取引があったのが9日だけなら判定不可
    rows = _series([100 - i for i in range(20)])
    for r in rows[9:]:
        r["trade_count"] = 0
    m2 = pkc.compute_metrics(rows)
    assert m2["valid_days"] == 9
    assert pkc.classify_trend(m2) == "判定不可"


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

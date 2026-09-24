"""地図を実データから描く処理の検証（検知すべきもの・見逃すべきもの）。"""
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import map_renderer as m

W, H = 960, 540


def rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def near(pixel, hex_color, tol=18):
    return all(abs(a - b) <= tol for a, b in zip(pixel, rgb(hex_color)))


def pixel_at(image, info, lon, lat):
    x, y = info["_view"].project(lon, lat)
    return image.getpixel((int(x), int(y)))


def render(spec, excerpt="", **kw):
    image, info = m.render_map(spec, excerpt, kw.pop("terms", []), width=W, height=H, **kw)
    assert image is not None, info
    return image, info


def test_caspian_sea_stays_one_sea_and_countries_come_from_data():
    image, info = render({"focus": ["KAZ", "TKM", "AZE", "IRN"],
                          "highlight": [{"a3": "KAZ", "tone": "compare"}, {"a3": "IRN", "tone": "neutral"}]},
                         "カスピ海", no_text=True)
    assert near(pixel_at(image, info, 50.5, 42.0), m.SEA)          # カスピ海の中央は海
    assert near(pixel_at(image, info, 67.0, 48.0), m.TONES["compare"])  # カザフスタンの内陸
    assert near(pixel_at(image, info, 54.0, 32.5), m.TONES["neutral"])  # イランの内陸
    assert near(pixel_at(image, info, 34.0, 43.0), m.SEA)          # 黒海も海


def owners_in_box(x0, y0, x1, y1):
    found = set()
    for a3, country in m.load_countries().items():
        for bx0, by0, bx1, by1 in country["bboxes"]:
            if bx0 >= x0 and bx1 <= x1 and by0 >= y0 and by1 <= y1:
                found.add(a3)
    return found


def test_borders_follow_japans_position():
    # 北方領土（択捉・国後・色丹・歯舞）は日本。ロシアの図形は含まれない
    assert owners_in_box(145.3, 43.1, 149.0, 45.6) == {"JPN"}
    # 竹島・尖閣も日本
    assert owners_in_box(131.7, 37.1, 132.0, 37.4) == {"JPN"}
    assert owners_in_box(123.3, 25.6, 123.8, 26.0) == {"JPN"}
    # クリミアはウクライナ（ロシアの図形の中に入らない）
    crimea = (34.1, 44.95)
    ukr = m.load_countries()["UKR"]
    rus = m.load_countries()["RUS"]
    assert any(m._inside(*crimea, poly[0]) for poly in ukr["polys"])
    assert not any(m._inside(*crimea, poly[0]) for poly in rus["polys"])


def test_northern_territories_are_not_painted_as_russia():
    image, info = render({"focus": ["JPN"], "highlight": [{"a3": "RUS", "tone": "main"},
                                                          {"a3": "JPN", "tone": "warn"}]}, "ロシアと日本",
                         no_text=True)
    assert near(pixel_at(image, info, 147.9, 45.05), m.TONES["warn"], tol=40)  # 択捉島は日本の色


def test_japan_is_drawn_once_and_sea_of_japan_is_sea():
    image, info = render({"focus": ["JPN", "KOR"], "highlight": [{"a3": "JPN", "tone": "warn"}]}, "日本",
                         no_text=True)
    assert near(pixel_at(image, info, 138.5, 36.3), m.TONES["warn"])  # 本州の内陸
    assert near(pixel_at(image, info, 134.5, 40.0), m.SEA)            # 日本海
    assert near(pixel_at(image, info, 127.8, 36.3), m.LAND)           # 韓国（強調なし）


def test_russia_uses_pacific_frame_so_chukotka_and_alaska_are_not_cut():
    image, info = render({"focus": ["RUS"], "highlight": [{"a3": "RUS", "tone": "main"},
                                                          {"a3": "USA", "tone": "compare"}]}, "ロシアとアメリカ",
                         no_text=True)
    assert info["view"]["frame"] == "pacific"
    assert near(pixel_at(image, info, 175.0, 66.8), m.TONES["main"])            # チュクチ（東経）
    assert near(pixel_at(image, info, -172.0 + 360, 66.0), m.TONES["main"])  # チュクチ（西経側）
    assert near(pixel_at(image, info, -160.0 + 360, 62.0), m.TONES["compare"])   # アラスカ


def test_pin_outside_its_country_is_dropped_and_inside_pin_is_drawn():
    _image, info = render({"focus": ["JPN"], "highlight": [{"a3": "JPN", "tone": "main"}],
                           "pins": [{"name": "東京", "country": "JPN", "lon": 150.0, "lat": 30.0},
                                    {"name": "札幌", "country": "JPN", "lon": 141.35, "lat": 43.06}]},
                          "東京と札幌")
    assert info["pins"] == ["札幌"]
    assert any("東京" in note and "範囲外" in note for note in info["notes"])


def test_pins_frame_the_story_region_instead_of_all_of_russia():
    _image, info = render({"focus": ["RUS"], "highlight": [{"a3": "RUS", "tone": "main"}],
                           "pins": [{"name": "旅順", "country": "CHN", "lon": 121.26, "lat": 38.81},
                                    {"name": "南樺太", "country": "RUS", "lon": 142.7, "lat": 47.0}]},
                          "旅順と南樺太", no_text=True)
    lon0, lon1 = info["view"]["lon"]
    assert 100 < lon0 < 121.26 and 142.7 < lon1 < 170  # 北東アジアの範囲（ロシア全体ではない）
    assert info["pins"] == ["旅順", "南樺太"]


def test_coastal_pin_just_off_the_polygon_is_kept():
    # 旅順は海岸線ぎわ。多少のずれは許容して描く。
    _image, info = render({"focus": ["CHN"], "pins": [{"name": "旅順", "country": "CHN",
                                                       "lon": 121.26, "lat": 38.81}]}, "旅順")
    assert info["pins"] == ["旅順"]


def test_label_override_only_when_word_is_in_excerpt():
    spec, notes = m.normalize_spec({"highlight": [{"a3": "CHN"}], "label_overrides": {"CHN": "清"}},
                                   "当時の清は")
    assert spec["label_overrides"] == {"CHN": "清"}
    spec, notes = m.normalize_spec({"highlight": [{"a3": "CHN"}], "label_overrides": {"CHN": "満州国"}},
                                   "当時の清は")
    assert spec["label_overrides"] == {}
    assert any("満州国" in n for n in notes)


def test_pin_name_not_in_excerpt_is_marker_only():
    spec, notes = m.normalize_spec({"focus": ["JPN"], "pins": [{"name": "大阪", "country": "JPN",
                                                                "lon": 135.5, "lat": 34.7}]}, "日本の都市")
    assert spec["pins"][0]["show_name"] is False
    assert any("大阪" in n for n in notes)


def test_fallback_uses_country_names_in_excerpt_with_longest_match():
    assert m.countries_in_text("ロシアとウクライナ、インドネシア", m.load_countries()) == ["RUS", "UKR", "IDN"]
    spec, notes = m.normalize_spec({}, "ロシアとウクライナの関係")
    assert spec["focus"] == ["RUS", "UKR"]
    assert [h["a3"] for h in spec["highlight"]] == ["RUS", "UKR"]
    assert any("抜粋の国名" in n for n in notes)


def test_unknown_codes_are_reported_not_drawn():
    spec, notes = m.normalize_spec({"focus": ["XXX", "JPN"], "highlight": [{"a3": "ZZZ"}]}, "")
    assert spec["focus"] == ["JPN"]
    assert spec["highlight"] == []
    assert any("XXX" in n for n in notes) and any("ZZZ" in n for n in notes)


def test_no_country_means_no_map_instead_of_a_wrong_one(tmp_path):
    image, info = m.render_map({}, "抽象的な概念の説明", [], width=W, height=H)
    assert image is None
    ok, error, _notes = m.render_map_file({}, tmp_path / "map.png", excerpt="抽象的な概念の説明")
    assert not ok and "国名" in error
    assert not (tmp_path / "map.png").exists()


def test_no_text_mode_draws_no_labels():
    _image, info = render({"focus": ["JPN"], "highlight": [{"a3": "JPN"}],
                           "pins": [{"name": "札幌", "country": "JPN", "lon": 141.35, "lat": 43.06}]},
                          "日本の札幌", no_text=True)
    assert info["labels_drawn"] == 0
    assert info["pins"] == ["札幌"]  # 印は描く


def test_render_map_file_writes_full_hd_png(tmp_path):
    ok, error, notes = m.render_map_file({"focus": ["UKR"], "highlight": [{"a3": "UKR"}]},
                                         tmp_path / "map.png", excerpt="ウクライナ")
    assert ok and error == ""
    with Image.open(tmp_path / "map.png") as img:
        assert img.size == (1920, 1080)
    assert [p.name for p in tmp_path.iterdir()] == ["map.png"]


def test_arrow_with_unknown_end_is_skipped():
    spec, notes = m.normalize_spec({"focus": ["RUS"], "arrows": [{"from": "RUS", "to": "どこか"}]}, "ロシア")
    assert spec["arrows"] == []
    assert any("矢印" in n for n in notes)


@pytest.mark.parametrize("value", ["off", "OFF"])
def test_renderer_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("ZUKAI_MAP_RENDERER", value)
    assert m.enabled() is False

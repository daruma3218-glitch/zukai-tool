#!/usr/bin/env python3
"""地図を実データ（Natural Earth の国境・公有。日本の立場版）から描く（2026-09-24）。

画像生成AIに地図を描かせると、黒海の形が崩れる・カスピ海が2つ・日本が2つ、といった誤りが出る
（ロシア解体新書のディレクター報告）。この描画は国の形と位置をすべて国境データから描き、
AIには「どの国を映すか・どの国を何色で強調するか・ピン・矢印」だけを map_spec で決めさせる。

map_spec（すべて任意）:
  focus:      画面に収める国 [ISO A3, ...]（最大8）
  bbox:       [西経度, 南緯度, 東経度, 北緯度]（focus の代わりに範囲を直接指定する時）
  highlight:  [{"a3": "RUS", "tone": "main"}, ...]  tone = main / compare / neutral / warn / attention
  labels:     強調した国に日本語の国名を付けるか（既定 true）
  label_overrides: {"CHN": "清"}  抜粋に実在する語だけ採用
  pins:       [{"name": "旅順", "country": "CHN", "lon": 121.26, "lat": 38.81}]（最大6）
  arrows:     [{"from": "RUS", "to": "KOR", "tone": "warn"}]（国コードかピン名、最大4）

国の形はデータ由来なので誤らない。ピンの座標はAI由来なので、指定国の範囲内（沿岸は少し外まで許容）に
あるかを国境データで確かめ、合わないものは描かずに notes に残す。仕様が読めない時は、抜粋に出てくる
国名から強調する国を決める。国境は現代のもの（歴史の国境は扱わない）。

国境データは Natural Earth の「日本の立場（point of view: JPN）」版。北方領土・竹島・尖閣は日本、
クリミアはウクライナとして描く（実効支配版ではロシア扱いになるため、日本向けの動画では使わない）。
"""

import json
import math
import os
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "geodata" / "countries.json"
FONT_PATH = ROOT / "assets" / "fonts" / "NotoSansJP-Bold.ttf"
WIDTH, HEIGHT = 1920, 1080
SUPERSAMPLE = 2
LAT_MIN, LAT_MAX = -60.0, 82.0
MAIN_PART_RATIO = 0.15  # 本土に対してこれより小さい離島・海外領土は画面の範囲決めに使わない

TONES = {"main": "#1B365D", "compare": "#2C4C3B", "neutral": "#E5A91A",
         "warn": "#A6192E", "attention": "#B7950B"}
DARK_TONES = {"main", "compare", "warn"}
FALLBACK_TONES = ["main", "warn", "compare", "neutral", "attention"]
SEA = "#D9E1E8"
LAND = "#F4F2ED"
LAND_EDGE = "#B8C4CE"
HIGHLIGHT_EDGE = "#FFFFFF"
TEXT_DARK = "#1B365D"
TEXT_LIGHT = "#FFFFFF"
PIN_FILL = "#A6192E"
# 動画のテロップで使う短い国名（Natural Earth の NAME_JA は正式名のため）
SHORT_NAMES = {"CHN": "中国", "KOR": "韓国", "PRK": "北朝鮮", "TWN": "台湾", "MNG": "モンゴル",
               "USA": "アメリカ", "ZAF": "南アフリカ", "CAF": "中央アフリカ", "KOS": "コソボ",
               "CYN": "北キプロス"}
MAX_FOCUS, MAX_HIGHLIGHT, MAX_PINS, MAX_ARROWS = 8, 12, 6, 4
PIN_TOLERANCE_DEG = 0.6
UNDETERMINED_CODES = ("XSS", "XKR")  # 南樺太・千島列島（日本政府の立場では帰属未定。どの国の色も塗らない）


def enabled() -> bool:
    return os.environ.get("ZUKAI_MAP_RENDERER", "on").strip().lower() != "off"


# ===== データ =====
def _ring_area(ring) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2


def _ring_bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(x, y, ring) -> bool:
    result = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            result = not result
    return result


def _label_point(poly):
    """外周の面積重心。凹んだ形で外に出る時は、中央の高さで最も長い区間の中点を使う。"""
    ring = poly[0]
    area = xs = ys = 0.0
    for a, b in zip(ring, ring[1:] + ring[:1]):
        cross = a[0] * b[1] - b[0] * a[1]
        area += cross
        xs += (a[0] + b[0]) * cross
        ys += (a[1] + b[1]) * cross
    if abs(area) > 1e-12:
        point = (xs / (3 * area), ys / (3 * area))
        if _inside(point[0], point[1], ring):
            return point
    x0, y0, x1, y1 = _ring_bbox(ring)
    y = (y0 + y1) / 2
    crossings = sorted(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                       for a, b in zip(ring, ring[1:] + ring[:1]) if (a[1] > y) != (b[1] > y))
    segments = [(crossings[i], crossings[i + 1]) for i in range(0, len(crossings) - 1, 2)]
    if segments:
        left, right = max(segments, key=lambda s: s[1] - s[0])
        return ((left + right) / 2, y)
    return ((x0 + x1) / 2, y)


@lru_cache(maxsize=1)
def load_countries() -> dict:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    countries = {}
    for feature in data["features"]:
        polys = [poly for poly in feature["polys"] if poly and len(poly[0]) >= 3]
        if not polys:
            continue
        areas = [_ring_area(poly[0]) for poly in polys]
        main_index = areas.index(max(areas))
        countries[feature["a3"]] = {
            "a3": feature["a3"],
            "ja": SHORT_NAMES.get(feature["a3"], feature["ja"]),
            "ja_formal": feature["ja"],
            "en": feature["en"],
            "polys": polys,
            "bboxes": [_ring_bbox(poly[0]) for poly in polys],
            "areas": areas,
            "main_area": areas[main_index],
            "label": _label_point(polys[main_index]),
            "undetermined": bool(feature.get("undetermined")),
        }
    return countries


# ===== 画面の範囲と投影 =====
def _shift(lon: float, frame: str, ring_max: float) -> float:
    """太平洋中心の図では、西半球だけにある図形を +360 して東アジアの右側に並べる。"""
    return lon + 360 if frame == "pacific" and ring_max <= 0 else lon


def _country_extent(country: dict, frame: str):
    boxes = []
    for bbox, area in zip(country["bboxes"], country["areas"]):
        if area < country["main_area"] * MAIN_PART_RATIO:
            continue
        x0, y0, x1, y1 = bbox
        boxes.append((_shift(x0, frame, x1), y0, _shift(x1, frame, x1), y1))
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _merc(lat: float) -> float:
    lat = max(min(lat, LAT_MAX), LAT_MIN)
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _inverse_merc(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


class View:
    def __init__(self, lon0, lon1, lat0, lat1, frame, width, height):
        self.frame = frame
        self.width, self.height = width, height
        cx0, cx1 = math.radians(lon0), math.radians(lon1)
        cy0, cy1 = _merc(lat0), _merc(lat1)
        span_x, span_y = cx1 - cx0, cy1 - cy0
        ratio = width / height
        if span_x / span_y > ratio:  # 横に長い → 上下を広げる
            grow = span_x / ratio - span_y
            cy0, cy1 = cy0 - grow / 2, cy1 + grow / 2
        else:
            grow = span_y * ratio - span_x
            cx0, cx1 = cx0 - grow / 2, cx1 + grow / 2
        low, high = _merc(LAT_MIN), _merc(LAT_MAX)
        if cy0 < low:
            cy1, cy0 = min(cy1 + (low - cy0), high), low
        if cy1 > high:
            cy0, cy1 = max(cy0 - (cy1 - high), low), high
        # 余白を足した結果が経度180度を越えたら、太平洋中心の座標で描く（アラスカ・チュクチ側を欠かさない）。
        if math.degrees(cx0) < -180:
            cx0, cx1 = cx0 + 2 * math.pi, cx1 + 2 * math.pi
        self.frame = "pacific" if math.degrees(cx1) > 180 else "atlantic"
        self.x0, self.x1, self.y0, self.y1 = cx0, cx1, cy0, cy1
        self.lon0, self.lon1 = math.degrees(cx0), math.degrees(cx1)
        self.lat0, self.lat1 = _inverse_merc(cy0), _inverse_merc(cy1)

    def project(self, lon: float, lat: float):
        x = (math.radians(lon) - self.x0) / (self.x1 - self.x0) * self.width
        y = (self.y1 - _merc(lat)) / (self.y1 - self.y0) * self.height
        return x, y

    def intersects(self, bbox) -> bool:
        x0, y0, x1, y1 = bbox
        return not (x1 < self.lon0 or x0 > self.lon1 or y1 < self.lat0 or y0 > self.lat1)

    def as_dict(self) -> dict:
        return {"frame": self.frame, "lon": [round(self.lon0, 2), round(self.lon1, 2)],
                "lat": [round(self.lat0, 2), round(self.lat1, 2)]}


def _make_view(bounds, frame, width, height, min_lon=6.0, min_lat=4.0) -> "View":
    lon0, lat0, lon1, lat1 = bounds
    span_lon = max(lon1 - lon0, min_lon)
    span_lat = max(lat1 - lat0, min_lat)
    cx, cy = (lon0 + lon1) / 2, (lat0 + lat1) / 2
    lon0, lon1 = cx - span_lon * 0.6, cx + span_lon * 0.6
    lat0, lat1 = max(cy - span_lat * 0.62, LAT_MIN), min(cy + span_lat * 0.62, LAT_MAX)
    return View(lon0, lon1, lat0, lat1, frame, width, height)


GIANT_SPAN_DEG = 30.0  # これより大きい国（ロシア・中国など）は、ピンがある時は範囲決めに使わない


def choose_view(a3_list, countries, width=WIDTH, height=HEIGHT, pins=None) -> "View":
    """強調国が収まる範囲。経度180度をまたぐ国（ロシアなど）は太平洋中心の図にする。

    ピン（話の舞台）がある時は、ロシアのような大国の全体ではなく、ピンと小さめの国が収まる範囲にする
    （極東の話なら北東アジア、黒海の話なら黒海周辺）。大国はその範囲の中で強調して見せる。
    """
    points = [(p["lon"], p["lat"]) for p in pins or []]
    best = None
    for frame in ("atlantic", "pacific"):
        extents = [_country_extent(countries[a3], frame) for a3 in a3_list]
        if points:
            extents = [e for e in extents if e[2] - e[0] <= GIANT_SPAN_DEG and e[3] - e[1] <= GIANT_SPAN_DEG]
            extents += [(lon + 360 if frame == "pacific" and lon < 0 else lon, lat) * 2 for lon, lat in points]
        bounds = (min(e[0] for e in extents), min(e[1] for e in extents),
                  max(e[2] for e in extents), max(e[3] for e in extents))
        span = bounds[2] - bounds[0]
        if best is None or span < best[0] - 1e-6:
            best = (span, frame, bounds)
    if points:
        return _make_view(best[2], best[1], width, height, min_lon=14.0, min_lat=8.0)
    return _make_view(best[2], best[1], width, height)


# ===== 仕様の確認 =====
def _excerpt_terms(excerpt: str, allowed_terms) -> str:
    return (excerpt or "") + " " + " ".join(t for t in (allowed_terms or []) if isinstance(t, str))


def countries_in_text(text: str, countries: dict) -> list:
    """文中の国名（短い名前・正式名）を、長い名前を優先して出てきた順に拾う。"""
    names = []
    for country in countries.values():
        for name in {country["ja"], country["ja_formal"]}:
            if len(name) >= 2:
                names.append((name, country["a3"]))
    names.sort(key=lambda item: -len(item[0]))
    taken = [False] * len(text)
    found = []
    for name, a3 in names:
        start = text.find(name)
        while start != -1:
            span = range(start, start + len(name))
            if not any(taken[i] for i in span):
                for i in span:
                    taken[i] = True
                found.append((start, a3))
            start = text.find(name, start + 1)
    ordered = []
    for _pos, a3 in sorted(found):
        if a3 not in ordered:
            ordered.append(a3)
    return ordered


def _near_country(lon: float, lat: float, country: dict) -> bool:
    for poly, bbox in zip(country["polys"], country["bboxes"]):
        x0, y0, x1, y1 = bbox
        if not (x0 - PIN_TOLERANCE_DEG <= lon <= x1 + PIN_TOLERANCE_DEG
                and y0 - PIN_TOLERANCE_DEG <= lat <= y1 + PIN_TOLERANCE_DEG):
            continue
        if _inside(lon, lat, poly[0]):
            return True
        if any(abs(px - lon) <= PIN_TOLERANCE_DEG and abs(py - lat) <= PIN_TOLERANCE_DEG for px, py in poly[0]):
            return True
    return False


def _pin_is_plausible(pin: dict, countries: dict) -> bool:
    code = pin.get("country") or ""
    country = countries.get(code)
    if not country:
        return True  # 国の指定が無いピンは、画面内にあるかだけを後で確かめる
    lon, lat = pin["lon"], pin["lat"]
    if _near_country(lon, lat, country):
        return True
    # 南樺太・千島列島（帰属未定）の地名は、ロシアや日本として指定されていても位置は正しい
    return code in ("RUS", "JPN") and any(
        countries.get(u) and _near_country(lon, lat, countries[u]) for u in UNDETERMINED_CODES)


def normalize_spec(spec, excerpt: str = "", allowed_terms=None) -> tuple:
    """map_spec を確かめて (整えた仕様, notes) を返す。描けない時は focus が空になる。"""
    countries = load_countries()
    notes = []
    spec = spec if isinstance(spec, dict) else {}
    text = _excerpt_terms(excerpt, allowed_terms)

    def codes(values, limit):
        result = []
        for value in values if isinstance(values, list) else []:
            code = str(value).strip().upper()
            if code in countries and code not in result:
                result.append(code)
            elif code:
                notes.append(f"国コード {code} は地図データに無いため使いません")
        return result[:limit]

    highlight = []
    for item in spec.get("highlight") if isinstance(spec.get("highlight"), list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("a3", "")).strip().upper()
        if code not in countries:
            if code:
                notes.append(f"国コード {code} は地図データに無いため強調しません")
            continue
        tone = item.get("tone") if item.get("tone") in TONES else "main"
        if code not in [h["a3"] for h in highlight]:
            highlight.append({"a3": code, "tone": tone})
    highlight = highlight[:MAX_HIGHLIGHT]
    focus = codes(spec.get("focus"), MAX_FOCUS) or [h["a3"] for h in highlight][:MAX_FOCUS]

    bbox = None
    raw_bbox = spec.get("bbox")
    if isinstance(raw_bbox, list) and len(raw_bbox) == 4 and all(isinstance(v, (int, float)) for v in raw_bbox):
        lon0, lat0, lon1, lat1 = raw_bbox
        if -180 <= lon0 <= 180 and -180 <= lon1 <= 180 and LAT_MIN <= lat0 < lat1 <= LAT_MAX:
            bbox = [lon0, lat0, lon1 + 360 if lon1 < lon0 else lon1, lat1]
        else:
            notes.append("範囲（bbox）の値が不正なため使いません")

    if not focus and not bbox:
        found = countries_in_text(text, countries)[:MAX_FOCUS]
        if found:
            notes.append("地図の指定が無いため、抜粋の国名から描きました")
            focus = found
            highlight = [{"a3": a3, "tone": FALLBACK_TONES[i % len(FALLBACK_TONES)]}
                         for i, a3 in enumerate(found)]

    overrides = {}
    raw_overrides = spec.get("label_overrides") if isinstance(spec.get("label_overrides"), dict) else {}
    for code, label in raw_overrides.items():
        code = str(code).upper()
        if code in countries and isinstance(label, str) and label.strip() and label.strip() in text:
            overrides[code] = label.strip()
        elif code in countries:
            notes.append(f"国名の置き換え「{label}」は抜粋に無いため使いません")

    # 強調した国と同じ名前のピン（例: ロシア）は国名ラベルと重なるので描かず、矢印はその国を指す
    label_to_a3 = {overrides.get(h["a3"], countries[h["a3"]]["ja"]): h["a3"] for h in highlight}
    pins = []
    for pin in spec.get("pins") if isinstance(spec.get("pins"), list) else []:
        if not isinstance(pin, dict):
            continue
        name = str(pin.get("name", "")).strip()
        if name and name in label_to_a3:
            notes.append(f"ピン「{name}」は強調した国の名前と同じため、国名ラベルにまとめました")
            if label_to_a3[name] not in focus and len(focus) < MAX_FOCUS:
                focus.append(label_to_a3[name])  # ピンの代わりに、その国を画面に収める
            continue
        lon, lat = pin.get("lon"), pin.get("lat")
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)) \
                or not (-180 <= lon <= 180 and LAT_MIN <= lat <= LAT_MAX):
            notes.append(f"ピン「{name}」は座標が不正なため描きません")
            continue
        entry = {"name": name, "country": str(pin.get("country", "")).strip().upper(),
                 "lon": float(lon), "lat": float(lat), "show_name": bool(name) and name in text}
        if not _pin_is_plausible(entry, countries):
            notes.append(f"ピン「{name}」は指定国の範囲外のため描きません（位置を確認してください）")
            continue
        if name and not entry["show_name"]:
            notes.append(f"ピン「{name}」の名前は抜粋に無いため、印だけ描きます")
        pins.append(entry)
    pins = pins[:MAX_PINS]

    arrows = []
    pin_names = {p["name"] for p in pins if p["name"]}
    for arrow in spec.get("arrows") if isinstance(spec.get("arrows"), list) else []:
        if not isinstance(arrow, dict):
            continue
        ends = []
        for key in ("from", "to"):
            value = str(arrow.get(key, "")).strip()
            ends.append(value.upper() if value.upper() in countries else label_to_a3[value] if value in label_to_a3
                        else value if value in pin_names else None)
        if None in ends or ends[0] == ends[1]:
            notes.append(f"矢印 {arrow.get('from')}→{arrow.get('to')} は始点か終点が不明なため描きません")
            continue
        tone = arrow.get("tone") if arrow.get("tone") in TONES else "warn"
        arrows.append({"from": ends[0], "to": ends[1], "tone": tone})
    arrows = arrows[:MAX_ARROWS]

    labels = spec.get("labels", True) is not False
    return ({"focus": focus, "bbox": bbox, "highlight": highlight, "labels": labels,
             "label_overrides": overrides, "pins": pins, "arrows": arrows}, notes)


# ===== 描画 =====
@lru_cache(maxsize=8)
def _font(size: int):
    try:
        return ImageFont.truetype(str(FONT_PATH), size)
    except OSError:
        return ImageFont.load_default()


def _text_box(draw, xy, text, font, stroke):
    left, top, right, bottom = draw.textbbox(xy, text, font=font, anchor="mm", stroke_width=stroke)
    return (left, top, right, bottom)


def _overlaps(box, boxes) -> bool:
    return any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]) for b in boxes)


MASK_SCALE = 10


def visible_anchor(country, view, width=WIDTH, height=HEIGHT):
    """画面に見えている部分の中心（画面座標）と、画面に占める割合。見えなければ (None, 0)。

    国名や矢印の端を、画面外にある国全体の重心ではなく、見えている部分に置くために使う。
    """
    mw, mh = max(width // MASK_SCALE, 1), max(height // MASK_SCALE, 1)
    mask = Image.new("L", (mw, mh), 0)
    draw = ImageDraw.Draw(mask)
    for poly, bbox in zip(country["polys"], country["bboxes"]):
        shifted = (_shift(bbox[0], view.frame, bbox[2]), bbox[1], _shift(bbox[2], view.frame, bbox[2]), bbox[3])
        if not view.intersects(shifted):
            continue
        for ring, fill in [(poly[0], 255)] + [(hole, 0) for hole in poly[1:]]:
            points = [(x / MASK_SCALE, y / MASK_SCALE) for x, y in
                      (view.project(_shift(lon, view.frame, bbox[2]), lat) for lon, lat in ring)]
            if len(points) >= 3:
                draw.polygon(points, fill=fill)
    count = sx = sy = 0
    pixels = mask.load()
    for y in range(mh):
        for x in range(mw):
            if pixels[x, y]:
                count += 1
                sx += x
                sy += y
    if not count:
        return None, 0.0
    cx, cy = sx / count, sy / count
    if not pixels[min(int(cx), mw - 1), min(int(cy), mh - 1)]:
        best = min(((x, y) for y in range(mh) for x in range(mw) if pixels[x, y]),
                   key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        cx, cy = best
    return ((cx + 0.5) * MASK_SCALE, (cy + 0.5) * MASK_SCALE), count / (mw * mh)


def _point_for(name, spec, countries, view, anchors):
    """矢印の端点。国コードなら見えている部分の中心、ピン名ならピンの位置（画面座標）。"""
    for pin in spec["pins"]:
        if pin["name"] == name:
            lon = pin["lon"] + 360 if view.frame == "pacific" and pin["lon"] < 0 else pin["lon"]
            return view.project(lon, pin["lat"])
    if name not in anchors:
        anchors[name] = visible_anchor(countries[name], view, view.width, view.height)
    anchor, _share = anchors[name]
    if anchor:
        return anchor
    country = countries[name]
    lon, lat = country["label"]
    main_box = country["bboxes"][country["areas"].index(country["main_area"])]
    return view.project(_shift(lon, view.frame, main_box[2]), lat)


def render_map(spec, excerpt: str = "", allowed_terms=None, no_text: bool = False,
               width: int = WIDTH, height: int = HEIGHT):
    """map_spec から地図画像を作る。戻り値 (Image か None, info)。"""
    countries = load_countries()
    spec, notes = normalize_spec(spec, excerpt, allowed_terms)
    if not spec["focus"] and not spec["bbox"] and not spec["pins"]:
        return None, {"notes": notes + ["地図にする国を決められませんでした（抜粋に国名も地名のピンもありません）"]}

    if spec["bbox"]:
        lon0, lat0, lon1, lat1 = spec["bbox"]
        frame = "pacific" if lon1 > 180 else "atlantic"
        view = _make_view((lon0, lat0, lon1, lat1), frame, width, height)
    else:
        view = choose_view(spec["focus"], countries, width, height, pins=spec["pins"])

    s = SUPERSAMPLE
    image = Image.new("RGB", (width * s, height * s), SEA)
    draw = ImageDraw.Draw(image)
    tones = {h["a3"]: h["tone"] for h in spec["highlight"]}

    def project_ring(ring, ring_max):
        return [(x * s, y * s) for x, y in (view.project(_shift(lon, view.frame, ring_max), lat)
                                            for lon, lat in ring)]

    visible = []
    for country in sorted(countries.values(), key=lambda c: -c["main_area"]):
        for poly, bbox in zip(country["polys"], country["bboxes"]):
            shifted = (_shift(bbox[0], view.frame, bbox[2]), bbox[1], _shift(bbox[2], view.frame, bbox[2]), bbox[3])
            if view.intersects(shifted):
                visible.append((country["a3"], poly, bbox[2]))

    # 面 → 境界線の順。強調しない国を先に塗り、強調国を上に重ねる。
    for highlighted in (False, True):
        for a3, poly, ring_max in visible:
            if (a3 in tones) != highlighted:
                continue
            fill = TONES[tones[a3]] if highlighted else LAND
            draw.polygon(project_ring(poly[0], ring_max), fill=fill)
            for hole in poly[1:]:
                draw.polygon(project_ring(hole, ring_max), fill=LAND)
    for highlighted in (False, True):
        for a3, poly, ring_max in visible:
            if (a3 in tones) != highlighted:
                continue
            ring = poly[0]
            points = project_ring(ring, ring_max)
            # 経度180度で分かれた図形の切れ目は国境ではないので線を引かない。
            run = []
            for i in range(len(ring) + 1):
                a, b = ring[i % len(ring)], ring[(i + 1) % len(ring)]
                run.append(points[i % len(ring)])
                if abs(a[0]) >= 179.999 and abs(b[0]) >= 179.999:
                    if len(run) >= 2:
                        draw.line(run, fill=HIGHLIGHT_EDGE if highlighted else LAND_EDGE,
                                  width=(3 if highlighted else 2) * s, joint="curve")
                    run = []
            if len(run) >= 2:
                draw.line(run, fill=HIGHLIGHT_EDGE if highlighted else LAND_EDGE,
                          width=(3 if highlighted else 2) * s, joint="curve")

    # 矢印
    anchors = {}
    for arrow in spec["arrows"]:
        (x0, y0), (x2, y2) = (_point_for(arrow["from"], spec, countries, view, anchors),
                              _point_for(arrow["to"], spec, countries, view, anchors))
        x0, y0, x2, y2 = x0 * s, y0 * s, x2 * s, y2 * s
        length = math.hypot(x2 - x0, y2 - y0)
        if length < 20 * s:
            notes.append(f"矢印 {arrow['from']}→{arrow['to']} は短すぎるため描きません")
            continue
        # 国名の上から出ないよう、国が始点の時は少し先から描き始める。
        if arrow["from"] in countries:
            step = min(length * 0.15, 70 * s)
            x0, y0 = x0 + (x2 - x0) / length * step, y0 + (y2 - y0) / length * step
            length = math.hypot(x2 - x0, y2 - y0)
        nx, ny = -(y2 - y0) / length, (x2 - x0) / length
        cx, cy = (x0 + x2) / 2 + nx * length * 0.18, (y0 + y2) / 2 + ny * length * 0.18
        head = 34 * s
        curve = []
        for i in range(41):
            t = i / 40
            curve.append(((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x2,
                          (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y2))
        ex, ey = curve[-1]
        px, py = curve[-4]
        angle = math.atan2(ey - py, ex - px)
        cut = [p for p in curve if math.hypot(p[0] - ex, p[1] - ey) > head * 0.8] or curve[:2]
        tip = [(ex, ey),
               (ex - head * math.cos(angle - 0.45), ey - head * math.sin(angle - 0.45)),
               (ex - head * math.cos(angle + 0.45), ey - head * math.sin(angle + 0.45))]
        draw.line(cut, fill="#FFFFFF", width=16 * s, joint="curve")
        draw.polygon(tip, fill="#FFFFFF", outline="#FFFFFF", width=5 * s)
        draw.line(cut, fill=TONES[arrow["tone"]], width=10 * s, joint="curve")
        draw.polygon(tip, fill=TONES[arrow["tone"]])

    # ピン
    pin_points = []
    for pin in spec["pins"]:
        lon = pin["lon"] + 360 if view.frame == "pacific" and pin["lon"] < 0 else pin["lon"]
        x, y = view.project(lon, pin["lat"])
        if not (0 <= x <= width and 0 <= y <= height):
            notes.append(f"ピン「{pin['name']}」は画面の外のため描きません")
            continue
        r = 13 * s
        draw.ellipse((x * s - r, y * s - r, x * s + r, y * s + r), fill=PIN_FILL, outline="#FFFFFF", width=5 * s)
        pin_points.append((pin, x * s, y * s))

    # 文字（「文字なし」の時は描かない）
    placed = []
    labels_drawn = 0
    if not no_text:
        if spec["labels"]:
            for a3, tone in tones.items():
                if a3 not in anchors:
                    anchors[a3] = visible_anchor(countries[a3], view, width, height)
                anchor, share = anchors[a3]
                text = spec["label_overrides"].get(a3, countries[a3]["ja"])
                if not anchor:
                    notes.append(f"国名「{text}」は画面の外のため省きました")
                    continue
                x = min(max(anchor[0], 120), width - 120) * s
                y = min(max(anchor[1], 50), height - 50) * s
                # 小さく見えている国は文字も小さくして、周りの国を隠しすぎないようにする。
                font = _font((46 if share >= 0.02 else 34) * s)
                dark = tone in DARK_TONES and share >= 0.004
                box = _text_box(draw, (x, y), text, font, 6 * s)
                if _overlaps(box, placed):
                    notes.append(f"国名「{text}」は他の文字と重なるため省きました")
                    continue
                draw.text((x, y), text, font=font, anchor="mm",
                          fill=TEXT_LIGHT if dark else TEXT_DARK,
                          stroke_width=6 * s, stroke_fill=TEXT_DARK if dark else TEXT_LIGHT)
                placed.append(box)
                labels_drawn += 1
        for pin, x, y in pin_points:
            if not pin["show_name"]:
                continue
            font = _font(38 * s)
            for dx, anchor in ((28 * s, "lm"), (-28 * s, "rm")):
                box = draw.textbbox((x + dx, y), pin["name"], font=font, anchor=anchor, stroke_width=6 * s)
                if box[0] >= 0 and box[2] <= width * s and not _overlaps(box, placed):
                    draw.text((x + dx, y), pin["name"], font=font, anchor=anchor, fill=TEXT_DARK,
                              stroke_width=6 * s, stroke_fill=TEXT_LIGHT)
                    placed.append(box)
                    labels_drawn += 1
                    break
            else:
                notes.append(f"ピン「{pin['name']}」の名前は他の文字と重なるため省きました")

    result = image.resize((width, height), Image.LANCZOS)
    info = {"view": view.as_dict(), "focus": spec["focus"], "highlight": spec["highlight"],
            "pins": [p["name"] for p, _x, _y in pin_points], "arrows": len(spec["arrows"]),
            "labels_drawn": labels_drawn, "notes": notes, "_view": view}
    return result, info


def render_map_file(spec, output_path: Path, excerpt: str = "", allowed_terms=None,
                    no_text: bool = False) -> tuple:
    """地図を描いてPNGで保存する。戻り値 (成功, エラー文, notes)。"""
    try:
        image, info = render_map(spec, excerpt, allowed_terms, no_text)
    except Exception as exc:  # 描画の失敗は1枚の失敗として返し、ジョブ全体は止めない
        return False, f"地図の描画に失敗: {str(exc)[:160]}", []
    notes = info.get("notes", [])
    if image is None:
        return False, notes[-1] if notes else "地図を描けませんでした", notes
    output_path = Path(output_path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=f".{output_path.name}.",
                                         suffix=".tmp", delete=False) as temp:
            temp_path = Path(temp.name)
        image.save(temp_path, format="PNG")
        os.replace(temp_path, output_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return True, "", notes

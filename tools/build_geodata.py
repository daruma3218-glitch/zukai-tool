#!/usr/bin/env python3
"""Natural Earth の国境（公有）から、地図描画用の軽いデータ geodata/countries.json を作る。

既定の元データは「日本の立場版」(point of view: JPN) の 1:10m 国境。北方領土・竹島・尖閣は日本、
クリミアはウクライナとして描かれる。実効支配版（1:50m）も --source defacto で作れる。

使い方:
  python tools/build_geodata.py <ne_10m_admin_0_countries_jpn.geojson>
  python tools/build_geodata.py --source defacto <ne_50m_admin_0_countries.geojson>

元データは nvkelso/natural-earth-vector の固定コミットのファイルで、SHA-256 を照合してから変換する。
国ごと（ISO A3）に図形をまとめ、線を間引き（Douglas-Peucker）、座標は小数第3位（約100m）に丸める。

日本の立場版では、さらに南樺太（北緯50度以南の樺太と付属の島）と千島列島（得撫島〜占守島）を
ロシアから外し、「帰属未定」として別に持つ（2026-09-24）。南樺太は XSS、千島列島は XKR と分け、
話に合わせて片方だけを強調できるようにする（日露戦争で割譲されたのは南樺太だけ、など）。日本政府はこの地域の帰属を
未定としており、教科書・地図帳もどの国の色も塗らない（1969年の文部省通達）。Natural Earth の
日本の立場版はここをロシアに含めているため、ロシアを塗ると一緒に塗られてしまう。
"""
import hashlib
import json
import sys
from pathlib import Path

COMMIT = "ca96624a56bd078437bca8184e78163e5039ad19"
SOURCES = {
    "jpn": {"file": "ne_10m_admin_0_countries_jpn.geojson",
            "sha256": "11bc047064a5cf2db03efc2aece341e657df2dd59ad715a8221cc1575697df1b",
            "tolerance": 0.02, "label": "Natural Earth 1:10m Admin 0 Countries, point of view: Japan"},
    "defacto": {"file": "ne_50m_admin_0_countries.geojson",
                "sha256": "3e458fc036ad0a66411f2c1e6cac49c5d7bfb81cb1123bc513b22511a2b7fdeb",
                "tolerance": 0.0, "label": "Natural Earth 1:50m Admin 0 Countries (de facto)"},
}
OUT = Path(__file__).resolve().parents[1] / "geodata" / "countries.json"

UNDETERMINED = {
    "XSS": {"a3": "XSS", "ja": "南樺太", "en": "South Sakhalin (undetermined)"},
    "XKR": {"a3": "XKR", "ja": "千島列島", "en": "Kuril Islands, Urup to Shumshu (undetermined)"},
}
SAKHALIN_BOX = (141.0, 45.5, 145.5, 54.6)   # 樺太と付属の島（大陸の海岸は図形が大きいので入らない）
SAKHALIN_BOUNDARY_LAT = 50.0                # 北緯50度以南が南樺太
KURIL_BOX = (149.0, 45.3, 157.0, 51.0)      # 得撫島〜占守島（北方四島は日本、カムチャツカ本土は入らない）


def _bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _within(bbox, box):
    return bbox[0] >= box[0] and bbox[1] >= box[1] and bbox[2] <= box[2] and bbox[3] <= box[3]


def clip_by_latitude(ring, lat, keep_south):
    """緯線で輪を切り、南側（または北側）だけを返す（Sutherland-Hodgman）。残らなければ None。"""
    points = ring[:-1] if ring and ring[0] == ring[-1] else list(ring)
    inside = (lambda p: p[1] <= lat) if keep_south else (lambda p: p[1] >= lat)
    out = []
    for i, cur in enumerate(points):
        prev = points[i - 1]
        if inside(cur) != inside(prev):
            t = (lat - prev[1]) / (cur[1] - prev[1])
            out.append([prev[0] + t * (cur[0] - prev[0]), lat])
        if inside(cur):
            out.append(list(cur))
    if len(out) < 3:
        return None
    return out + [out[0]]


def split_undetermined(polys):
    """ロシアの図形を (ロシアに残す, 南樺太, 千島列島) に分ける。polys は [[外周, 穴...], ...]。"""
    russia, south_sakhalin, kurils = [], [], []
    for poly in polys:
        bbox = _bbox(poly[0])
        if _within(bbox, KURIL_BOX):
            kurils.append(poly)
        elif _within(bbox, SAKHALIN_BOX) and bbox[3] <= SAKHALIN_BOUNDARY_LAT:
            south_sakhalin.append(poly)
        elif _within(bbox, SAKHALIN_BOX) and bbox[1] < SAKHALIN_BOUNDARY_LAT:
            for keep_south, target in ((True, south_sakhalin), (False, russia)):
                rings = [clip_by_latitude(ring, SAKHALIN_BOUNDARY_LAT, keep_south) for ring in poly]
                if rings[0]:
                    target.append([rings[0]] + [r for r in rings[1:] if r])
        else:
            russia.append(poly)
    return russia, south_sakhalin, kurils


def a3_of(props):
    return next((props[k] for k in ("ISO_A3_EH", "ISO_A3", "ADM0_A3")
                 if props.get(k) and props[k] != "-99"), None)


def ring_area(ring):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2


def _segment_distance(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return ((x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2) ** 0.5


def simplify(ring, tolerance):
    """Douglas-Peucker（反復）。小さな島も消さないよう、最低4点（閉じた三角形）は残す。"""
    if tolerance <= 0 or len(ring) <= 4:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        start, end = stack.pop()
        best, index = 0.0, None
        for i in range(start + 1, end):
            d = _segment_distance(ring[i], ring[start], ring[end])
            if d > best:
                best, index = d, i
        if index is not None and best > tolerance:
            keep[index] = True
            stack.extend(((start, index), (index, end)))
    result = [p for p, k in zip(ring, keep) if k]
    if len(result) < 4:  # 間引きすぎた小島は元の形のまま
        return ring
    return result


def main(argv):
    kind = "jpn"
    if len(argv) >= 2 and argv[0] == "--source":
        kind, argv = argv[1], argv[2:]
    if kind not in SOURCES or len(argv) != 1:
        raise SystemExit(__doc__)
    source = SOURCES[kind]
    raw = Path(argv[0]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != source["sha256"]:
        raise SystemExit(f"hash mismatch: {digest}")
    countries = {}
    points = 0
    for feature in json.loads(raw)["features"]:
        props, geom = feature["properties"], feature["geometry"]
        a3 = a3_of(props)
        if not a3 or not geom:
            continue
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        entry = countries.setdefault(a3, {"a3": a3, "ja": "", "en": "", "polys": [], "_best": -1.0})
        area = 0.0
        for poly in polys:
            entry["polys"].append([simplify(ring, source["tolerance"]) for ring in poly])
            area += ring_area(poly[0])
        if area > entry["_best"]:  # 同じ国コードの属領より本土の名前を使う
            entry.update(_best=area, ja=props.get("NAME_JA") or props.get("NAME") or a3,
                         en=props.get("NAME") or a3)
    if kind == "jpn" and "RUS" in countries:
        countries["RUS"]["polys"], south_sakhalin, kurils = split_undetermined(countries["RUS"]["polys"])
        for code, polys in (("XSS", south_sakhalin), ("XKR", kurils)):
            countries[code] = dict(UNDETERMINED[code], polys=polys, undetermined=True, _best=0.0)
    features = []
    for entry in countries.values():
        entry.pop("_best")
        entry["polys"] = [[[[round(x, 3), round(y, 3)] for x, y, *_ in ring] for ring in poly]
                          for poly in entry["polys"]]
        points += sum(len(ring) for poly in entry["polys"] for ring in poly)
        features.append(entry)
    features.sort(key=lambda e: e["a3"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = {"source": f"https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{COMMIT}/geojson/{source['file']}",
            "source_sha256": source["sha256"], "dataset": source["label"], "simplify_deg": source["tolerance"],
            "license": "public domain (Natural Earth)", "features": features}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(features)} countries, {points:,} points -> {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])

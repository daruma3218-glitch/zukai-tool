#!/usr/bin/env python3
"""Natural Earth 1:50m 国境（公有）から、地図描画用の軽いデータ geodata/countries_50m.json を作る。

使い方:
  python tools/build_geodata.py <ne_50m_admin_0_countries.geojson>

元データは nvkelso/natural-earth-vector の固定コミットのファイルで、SHA-256 を照合してから変換する。
国ごと（ISO A3）に図形をまとめ、座標は小数第3位（約100m）に丸める。
"""
import hashlib
import json
import sys
from pathlib import Path

SOURCE_COMMIT = "ca96624a56bd078437bca8184e78163e5039ad19"
SOURCE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/" + SOURCE_COMMIT
              + "/geojson/ne_50m_admin_0_countries.geojson")
SOURCE_SHA256 = "3e458fc036ad0a66411f2c1e6cac49c5d7bfb81cb1123bc513b22511a2b7fdeb"
OUT = Path(__file__).resolve().parents[1] / "geodata" / "countries_50m.json"


def a3_of(props):
    return next((props[k] for k in ("ISO_A3_EH", "ISO_A3", "ADM0_A3")
                 if props.get(k) and props[k] != "-99"), None)


def ring_area(ring):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2


def main(path):
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCE_SHA256:
        raise SystemExit(f"hash mismatch: {digest}")
    countries = {}
    for feature in json.loads(raw)["features"]:
        props, geom = feature["properties"], feature["geometry"]
        a3 = a3_of(props)
        if not a3:
            continue
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        entry = countries.setdefault(a3, {"a3": a3, "ja": "", "en": "", "polys": [], "_best": -1.0})
        area = 0.0
        for poly in polys:
            rings = [[[round(x, 3), round(y, 3)] for x, y in ring] for ring in poly]
            entry["polys"].append(rings)
            area += ring_area(poly[0])
        if area > entry["_best"]:  # 同じ国コードの属領より本土の名前を使う
            entry.update(_best=area, ja=props.get("NAME_JA") or props.get("NAME") or a3,
                         en=props.get("NAME") or a3)
    features = []
    for entry in countries.values():
        entry.pop("_best")
        features.append(entry)
    features.sort(key=lambda e: e["a3"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = {"source": SOURCE_URL, "source_sha256": SOURCE_SHA256, "license": "public domain (Natural Earth)",
            "features": features}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(features)} countries -> {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])

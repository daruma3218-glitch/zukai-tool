# 同梱データの出典

- `geodata/countries.json`: Natural Earth 1:10m Admin 0 Countries, point of view: Japan（パブリックドメイン）。nvkelso/natural-earth-vector コミット ca96624a56bd078437bca8184e78163e5039ad19 の ne_10m_admin_0_countries_jpn.geojson（SHA-256 11bc047064a5cf2db03efc2aece341e657df2dd59ad715a8221cc1575697df1b）を `tools/build_geodata.py` で間引き（0.02度）して変換。南樺太（北緯50度以南）と千島列島（得撫島〜占守島）はロシアから分けて「帰属未定」（南樺太 XSS・千島列島 XKR）とした。
- `assets/fonts/NotoSansJP-Bold.ttf`: Noto Sans JP（SIL Open Font License 1.1。全文は `assets/fonts/OFL.txt`）。

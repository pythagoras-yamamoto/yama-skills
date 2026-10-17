# yama-route-researcher のツール

日本の山のルート調査で使う Python の CLI ツールです。どれも JSON を標準出力に返し、ネットワークや API の失敗では exit 0 で `gaps` / `error` を返します（スキル全体を止めない）。

```bash
uv sync               # 依存関係のインストール
uv run pytest -q      # テスト（HTTP はモック。ネットワーク不要）
```

## find_peak.py — 山の特定

```bash
uv run python find_peak.py "駒ヶ岳"
uv run python find_peak.py "駒ヶ岳" --prefecture 長野県
```

- OpenStreetMap の `natural=peak|volcano` と国土地理院の地名検索で候補を探す
- 表記ゆれ（ヶ/ケ/が/ガ/ヵ、岳/嶽、竜/龍）を自動で試す
- 1km 以内の候補は同じ山として統合
- 各候補に都道府県・市町村（地理院の逆ジオコーダ＋気象庁地域コード）、標高（OSM、無ければ地理院 DEM）、地図リンクを付ける

## fetch_conditions.py — コンディション一括取得

```bash
uv run python fetch_conditions.py \
  --coordinates "36.3419,137.6476" --elevation 3180 --peak-name "槍ヶ岳" \
  [--date 2026-10-18] [--trailhead "36.2497,137.6352"] \
  [--distance-km 40 --up-m 1700 --down-m 1700] [--start-time 05:00 --finish-by 15:00] \
  [--body-kg 60 --pack-kg 10] [--waypoint "lat,lon" --waypoint "lat,lon"]
```

| キー | 内容 | データ源 |
|------|------|---------|
| `location` | 都道府県・市町村・気象庁の地域コード | 地理院 逆ジオコーダ、気象庁 `area.json` |
| `weather` | 7日間、山頂標高の気温・降水・風・稜線の風・体感温度・0℃高度・強風の目安 | Open-Meteo |
| `jma_forecast` | 府県天気予報（平地向け）の天気文・風・降水確率 | 気象庁 `bosai/forecast` |
| `jma_warnings` | 対象市町村の警報・注意報、データ鮮度の警告 | 気象庁 `bosai/warning/data/r8`（旧形式にも対応） |
| `air_quality` | PM2.5 日平均（注意喚起の目安 70μg/m³）、黄砂 | Open-Meteo Air Quality |
| `daylight` | 薄明・日の出・日の入り（Asia/Tokyo） | astral |
| `volcano` | 山頂15km以内の火山と気象庁の火山情報リンク | OSM |
| `municipalities` | 登山口→山頂の通過市町村 | 地理院、気象庁 |
| `protected_areas` | 国立公園・国定公園など | OSM |
| `nearest_hospital` / `police` | 病院・警察（登山口または山頂から） | OSM |
| `huts` / `campgrounds` | 山小屋・避難小屋（山頂8km以内）、キャンプ場（15km以内） | OSM |
| `time_estimates` | 速い/標準/ゆっくりの歩行時間、コース定数、エネルギー・脱水量 | DIN 33466、山本正嘉の式 |
| `itinerary` | 山頂到着・帰着・引き返し期限、15時到着・日没の判定 | 計算 |
| `bearings` | 区間ごとの方位（真北）と距離 | 計算 |

### 計算の根拠

- **歩行時間（DIN 33466）:** 水平 4km/h、登り 300m/h、下り 500m/h。水平と垂直の時間の大きい方＋小さい方の半分。日本のガイドブックのコースタイムに近い値になるが、山と高原地図・ヤマレコ・YAMAP のコースタイムがあればそちらを優先する。
- **コース定数（山本正嘉）:** 1.8×時間(h) ＋ 0.3×距離(km) ＋ 10.0×登り(km) ＋ 0.6×下り(km)。エネルギー消費量 ≒ コース定数 ×（体重＋荷物 kg）。
- **脱水量:** 体重(kg) × 行動時間(h) × 5 ml。
- **体感温度:** 風速 1m/s で約1℃下がる、という登山でよく使う経験則。
- **強風の目安:** 10m/s で歩きにくい、15m/s で稜線は危険、20m/s で行動困難。
- **0℃高度の注意:** 0℃高度が山頂から600m以内（`near_summit`）。

## cloudscrape.py — 取得しにくいページの取得

元プロジェクトのものをそのまま使っています。WebFetch が失敗したときの代わりです。

```bash
uv run python cloudscrape.py "https://example.com/page"
uv run python cloudscrape.py --render "https://example.com/js-page"
```

## 気象庁データについての注意

`www.jma.go.jp/bosai/` の JSON は公開仕様ではない内部データです。2026年5月28日の防災気象情報の新体系で、警報は `data/warning/` から `data/r8/` に移り、形式も「報のリスト」に変わりました（旧パスは更新が止まったまま残っています）。`fetch_conditions.py` は新形式を読み、`map_time.json` で更新が6時間以上止まっていれば `stale_warning` を出します。新形式が404になったら旧形式で読み、古い可能性を警告します。

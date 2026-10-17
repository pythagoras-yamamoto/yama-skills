---
name: conditions
description: 日本の山の天気・警報・火山・日の出日の入りをすばやく確認する
---

# コンディションの簡易確認

日本の山について、天気・気象庁の警報注意報・火山・PM2.5・日の出日の入り・緊急連絡先を取得します。山行記録の収集やエージェントの起動はしないので、フルのルート調査よりずっと速く終わります。

引数で山名が渡された場合（例: `/yama:conditions 赤岳`）はそれを対象にします。無ければどの山か聞いてください。

## 1. 山の特定

```bash
cd ${CLAUDE_PLUGIN_ROOT}/skills/yama-route-researcher/tools && uv run python find_peak.py "{山名}"
```

- **候補が複数:** AskUserQuestion で「{山名}（{標高}m、{都道府県}{市町村}）- {地理院地図リンク}」の形で選んでもらう。「その他」も入れる
- **1つ:** 「{山名}（{標高}m、{都道府県}）で合っていますか？」と確認
- **0件:** 表記ゆれ・別名で再検索。駄目ならユーザーに確認

## 2. コンディションの取得

```bash
cd ${CLAUDE_PLUGIN_ROOT}/skills/yama-route-researcher/tools && uv run python fetch_conditions.py \
  --coordinates "{緯度},{経度}" \
  --elevation {標高m} \
  --peak-name "{山名}"
  # 任意:
  # --date YYYY-MM-DD               日の出・日の入りを計算する日
  # --trailhead "lat,lon"           通過市町村・登山口周辺の施設
  # --distance-km N --up-m N        歩行時間・コース定数
  # --start-time HH:MM              行程表（距離・登りも必要）
  # --waypoint "lat,lon" ...        方位（2つ以上）
```

**スクリプトが失敗したら** 失敗を伝え、手動で確認するリンクを示す:

- 気象庁: `https://www.jma.go.jp/bosai/forecast/`
- tenki.jp 山の天気: `https://tenki.jp/mountain/`
- てんきとくらす: `https://tenkura.n-kishou.co.jp/tk/`
- 気象庁 火山: `https://www.data.jma.go.jp/vois/data/tokyo/STOCK/activity_info/index.html`

## 3. 結果の表示

短く、見やすく。天気と日の出日の入りは表にする。

1. **山:** 名前、標高、座標（地理院地図リンク）、都道府県・市町村
2. **警報・注意報:** `jma_warnings.active`（`mountain_relevant` のものを強調）。`stale_warning` があれば必ず表示。公式ページへのリンク
3. **天気（7日間）:** 日付・天気・最高/最低気温（体感）・降水・稜線の風（`wind_alert` は ⚠️）・0℃高度（`near_summit` は ⚠️）
4. **0℃高度の注意:** `near_summit=true` の日があれば目立たせる
5. **気象庁の府県天気予報:** 平地向けである旨を添えて
6. **大気:** PM2.5 が注意喚起の目安（日平均 70μg/m³）を超える日があるときだけ強調
7. **日の出・日の入り:** 薄明を含む表と日照時間
8. **火山:** `volcano.is_volcanic_area` が true なら、噴火警戒レベルを WebSearch（`"{火山名} 噴火警戒レベル site:jma.go.jp"`）で確認して書く
9. **緊急連絡先:** 病院（名前・距離・電話）、警察署、110/119。OSM のデータは不完全なことがある旨
10. **山小屋:** `huts` の近い順に数件
11. **歩行時間・コース定数**（あれば）
12. **行程表**（あれば）: `late_arrival` / `after_dark` が true なら目立つ警告
13. **方位**（あれば）

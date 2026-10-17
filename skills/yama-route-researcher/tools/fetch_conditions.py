#!/usr/bin/env python3
"""日本の山向け コンディション一括取得ツール。

山頂座標から、天気（Open-Meteo）・気象庁の府県天気予報と警報・注意報・
PM2.5/黄砂・日の出日の入り・近くの火山・通過市町村・病院・警察・山小屋・
キャンプ場・国立公園などをまとめて JSON で返す。

元プロジェクト（claude-mountaineering-skills）の fetch_conditions.py を日本向けに
作り直したもの。単位はメートル法（m, km, ℃, m/s）。
API の失敗は gaps に記録し、exit 0 で続行する。
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from typing import Any

import click

import jp_geo

OPEN_METEO_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
JMA_FORECAST_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/{office}.json"
# 2026-05-28 の防災気象情報の新体系で警報 JSON は data/r8/ に移った（旧 data/warning/ は凍結）
JMA_WARNING_URL = "https://www.jma.go.jp/bosai/warning/data/r8/{office}.json"
JMA_WARNING_MAP_TIME_URL = "https://www.jma.go.jp/bosai/warning/data/r8/map_time.json"
JMA_WARNING_LEGACY_URL = "https://www.jma.go.jp/bosai/warning/data/warning/{office}.json"
TIMEZONE = "Asia/Tokyo"

WEATHER_CODES = {
    0: ("快晴", "☀️"), 1: ("晴れ", "🌤️"), 2: ("晴れ時々くもり", "⛅"), 3: ("くもり", "☁️"),
    45: ("霧", "🌫️"), 48: ("着氷性の霧", "🌫️"),
    51: ("弱い霧雨", "🌧️"), 53: ("霧雨", "🌧️"), 55: ("強い霧雨", "🌧️"),
    56: ("着氷性の霧雨", "🌧️"), 57: ("強い着氷性の霧雨", "🌧️"),
    61: ("弱い雨", "🌧️"), 63: ("雨", "🌧️"), 65: ("強い雨", "🌧️"),
    66: ("着氷性の雨", "🌧️"), 67: ("強い着氷性の雨", "🌧️"),
    71: ("弱い雪", "❄️"), 73: ("雪", "❄️"), 75: ("強い雪", "❄️"), 77: ("霧雪", "❄️"),
    80: ("にわか雨", "🌦️"), 81: ("にわか雨", "🌧️"), 82: ("激しいにわか雨", "🌧️"),
    85: ("にわか雪", "🌨️"), 86: ("強いにわか雪", "🌨️"),
    95: ("雷雨", "⛈️"), 96: ("ひょうを伴う雷雨", "⛈️"), 99: ("強いひょうを伴う雷雨", "⛈️"),
}

# 気象庁 警報・注意報コード（TELOPS 準拠、2026-05-28 新体系のレベル付き名称）
JMA_WARNING_CODES = {
    "2": "暴風雪警報", "3": "レベル３大雨警報", "4": "洪水警報", "5": "暴風警報",
    "6": "大雪警報", "7": "波浪警報", "8": "レベル３高潮警報", "9": "レベル３土砂災害警報",
    "10": "レベル２大雨注意報", "12": "大雪注意報", "13": "風雪注意報", "14": "雷注意報",
    "15": "強風注意報", "16": "波浪注意報", "17": "融雪注意報", "18": "洪水注意報",
    "19": "レベル２高潮注意報", "20": "濃霧注意報", "21": "乾燥注意報", "22": "なだれ注意報",
    "23": "低温注意報", "24": "霜注意報", "25": "着氷注意報", "26": "着雪注意報",
    "27": "その他の注意報", "29": "レベル２土砂災害注意報",
    "32": "暴風雪特別警報", "33": "レベル５大雨特別警報", "35": "暴風特別警報",
    "36": "大雪特別警報", "37": "波浪特別警報", "38": "レベル５高潮特別警報",
    "39": "レベル５土砂災害特別警報",
    "43": "レベル４大雨危険警報", "48": "レベル４高潮危険警報", "49": "レベル４土砂災害危険警報",
}
# 登山で特に注目すべき警報・注意報
MOUNTAIN_RELEVANT_WARNINGS = {"2", "3", "5", "6", "9", "13", "14", "15", "17", "20", "22",
                              "23", "25", "26", "29", "32", "33", "35", "36", "39", "43", "49"}
ACTIVE_STATUSES = ("発表", "継続", "更新")


def warning_name(code: Any) -> str:
    try:
        return JMA_WARNING_CODES.get(str(int(str(code))), f"不明なコード({code})")
    except ValueError:
        return f"不明なコード({code})"


# ---------------------------------------------------------------- 天気（Open-Meteo）


def _pressure_level_for(elevation_m: float) -> int | None:
    """山頂標高に近い気圧面（稜線の風の目安）。"""
    if elevation_m >= 2200:
        return 700  # 約 3,000 m
    if elevation_m >= 1000:
        return 850  # 約 1,500 m
    return None


def fetch_weather(lat: float, lon: float, elevation_m: float, days: int = 7) -> dict[str, Any]:
    level = _pressure_level_for(elevation_m)
    hourly = ["freezing_level_height"]
    if level:
        hourly.append(f"wind_speed_{level}hPa")
    params = {
        "latitude": lat,
        "longitude": lon,
        "elevation": elevation_m,
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
            "precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max"
        ),
        "hourly": ",".join(hourly),
        "wind_speed_unit": "ms",
        "timezone": TIMEZONE,
        "forecast_days": days,
    }
    try:
        with jp_geo.client() as c:
            resp = c.get(OPEN_METEO_WEATHER_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        return {"forecast": [], "error": str(e)}

    daily = data.get("daily", {})
    hourly_data = data.get("hourly", {})
    fl_all = hourly_data.get("freezing_level_height") or []
    upper_all = hourly_data.get(f"wind_speed_{level}hPa") or [] if level else []

    def _day(arr: list, i: int) -> list:
        return [v for v in arr[i * 24 : (i + 1) * 24] if v is not None]

    def _get(key: str, i: int) -> Any:
        arr = daily.get(key) or []
        return arr[i] if i < len(arr) else None

    forecast = []
    for i, date_str in enumerate(daily.get("time", [])):
        code = _get("weather_code", i)
        label, icon = WEATHER_CODES.get(code, ("不明", "❓"))
        fl = _day(fl_all, i)
        upper = _day(upper_all, i)
        t_min = _get("temperature_2m_min", i)
        wind = _get("wind_speed_10m_max", i)
        upper_max = round(max(upper), 1) if upper else None
        ridge_wind = max(v for v in (wind, upper_max) if v is not None) if (wind or upper_max) else None
        forecast.append(
            {
                "date": date_str,
                "weekday": "月火水木金土日"[datetime.strptime(date_str, "%Y-%m-%d").weekday()],
                "conditions": f"{icon} {label}",
                "temp_max_c": _round(_get("temperature_2m_max", i)),
                "temp_min_c": _round(t_min),
                "precip_mm": _round(_get("precipitation_sum", i)),
                "precip_prob": _get("precipitation_probability_max", i),
                "wind_max_ms": _round(wind),
                "gust_max_ms": _round(_get("wind_gusts_10m_max", i)),
                "upper_wind_max_ms": upper_max,
                "upper_wind_level_hpa": level,
                # 体感温度の目安: 風速 1 m/s で約 1℃ 下がる（登山でよく使う経験則）
                "feels_like_min_c": _round(t_min - ridge_wind)
                if t_min is not None and ridge_wind is not None
                else None,
                "freezing_level_m": round(sum(fl) / len(fl)) if fl else None,
            }
        )
    return {
        "forecast": forecast,
        "timezone": data.get("timezone"),
        "model_elevation_m": elevation_m,
        "source_url": f"https://open-meteo.com/en/docs#latitude={lat}&longitude={lon}",
        "note": (
            "気温は山頂標高に補正済み。10m風速はモデル地形上の値で稜線では過小評価になりやすいため、"
            "upper_wind_max_ms（山頂付近の気圧面の風）も合わせて見ること。"
        ),
    }


def _round(v: Any, nd: int = 1) -> Any:
    return round(v, nd) if isinstance(v, (int, float)) else v


def enrich_forecast(forecast: list[dict], summit_m: float) -> None:
    """雪線（0℃高度）と強風の注意フラグを追加する。"""
    for day in forecast:
        fl = day.get("freezing_level_m")
        if fl is None:
            day["snow_line_note"] = "0℃高度データなし"
            day["near_summit"] = False
        else:
            diff = fl - summit_m
            day["near_summit"] = diff <= 600
            if diff <= 0:
                day["snow_line_note"] = f"0℃高度 約{fl:,}m（山頂より{abs(round(diff)):,}m低い＝山頂は氷点下）"
            else:
                day["snow_line_note"] = f"0℃高度 約{fl:,}m（山頂の{round(diff):,}m上）"
        ridge = max(
            (v for v in (day.get("wind_max_ms"), day.get("upper_wind_max_ms")) if v is not None),
            default=None,
        )
        # 目安: 10 m/s で歩きにくい、15 m/s で稜線歩行は危険、20 m/s で行動困難
        if ridge is None:
            day["wind_alert"] = None
        elif ridge >= 20:
            day["wind_alert"] = "行動困難（20m/s以上）"
        elif ridge >= 15:
            day["wind_alert"] = "稜線は危険（15m/s以上）"
        elif ridge >= 10:
            day["wind_alert"] = "強風注意（10m/s以上）"
        else:
            day["wind_alert"] = None


# ---------------------------------------------------------------- 大気（PM2.5・黄砂）


def fetch_air_quality(lat: float, lon: float, days: int = 5) -> dict[str, Any]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "pm2_5,dust",
        "timezone": TIMEZONE,
        "forecast_days": days,
    }
    try:
        with jp_geo.client() as c:
            resp = c.get(OPEN_METEO_AQ_URL, params=params)
            resp.raise_for_status()
            hourly = resp.json().get("hourly", {})
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}

    times = hourly.get("time") or []
    pm = hourly.get("pm2_5") or []
    dust = hourly.get("dust") or []
    by_day: dict[str, dict[str, list]] = {}
    for i, t in enumerate(times):
        d = by_day.setdefault(t[:10], {"pm": [], "dust": []})
        if i < len(pm) and pm[i] is not None:
            d["pm"].append(pm[i])
        if i < len(dust) and dust[i] is not None:
            d["dust"].append(dust[i])
    daily = []
    for date, d in by_day.items():
        pm_avg = round(sum(d["pm"]) / len(d["pm"]), 1) if d["pm"] else None
        daily.append(
            {
                "date": date,
                "pm25_daily_avg": pm_avg,
                "dust_max": round(max(d["dust"]), 1) if d["dust"] else None,
                # 環境省の注意喚起の暫定指針: 日平均 70 μg/m³ 超
                "pm25_alert": pm_avg is not None and pm_avg > 70,
            }
        )
    return {
        "daily": daily,
        "unit": "μg/m³",
        "note": "PM2.5 は環境省の注意喚起の暫定指針（日平均 70μg/m³ 超）で判定。黄砂は気象庁の黄砂情報も確認。",
        "links": {
            "soramame": "https://soramame.env.go.jp/",
            "jma_kosa": "https://www.data.jma.go.jp/env/kosateikyou/kosa.html",
        },
    }


# ---------------------------------------------------------------- 気象庁


def fetch_jma_forecast(office: str, class10: str | None) -> dict[str, Any]:
    """府県天気予報（3日分の天気文・風・降水確率）を一次細分区域単位で取り出す。"""
    url = JMA_FORECAST_URL.format(office=office)
    try:
        with jp_geo.client() as c:
            resp = c.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "page": _jma_forecast_page(office)}

    try:
        short = data[0]
        ts = short.get("timeSeries", [])
        areas = ts[0].get("areas", [])
        area = next((a for a in areas if a.get("area", {}).get("code") == class10), areas[0])
        days = [
            {
                "time": t,
                "weather": (area.get("weathers") or [None] * 9)[i],
                "wind": (area.get("winds") or [None] * 9)[i],
            }
            for i, t in enumerate(ts[0].get("timeDefines", []))
        ]
        pops: list[dict] = []
        if len(ts) > 1:
            p_areas = ts[1].get("areas", [])
            p_area = next((a for a in p_areas if a.get("area", {}).get("code") == class10), None)
            if p_area is None and p_areas:
                p_area = p_areas[0]
            if p_area:
                pops = [
                    {"time": t, "pop": p}
                    for t, p in zip(ts[1].get("timeDefines", []), p_area.get("pops", []))
                ]
        return {
            "publishing_office": short.get("publishingOffice"),
            "report_datetime": short.get("reportDatetime"),
            "area_name": area.get("area", {}).get("name"),
            "days": days,
            "pops": pops,
            "page": _jma_forecast_page(office),
            "note": "府県天気予報は平地（市街地）向け。山頂の天気は山の天気予報サービスも併用すること。",
        }
    except (IndexError, KeyError, AttributeError, TypeError) as e:
        return {"error": f"府県天気予報の形式が想定外: {e}", "page": _jma_forecast_page(office)}


def _jma_forecast_page(office: str) -> str:
    return f"https://www.jma.go.jp/bosai/forecast/#area_type=offices&area_code={office}"


def _jma_warning_page(class20: str | None, office: str) -> str:
    if class20:
        return f"https://www.jma.go.jp/bosai/warning/#area_type=class20s&area_code={class20}&lang=ja"
    return f"https://www.jma.go.jp/bosai/warning/#area_type=offices&area_code={office}&lang=ja"


def parse_warnings(data: Any, class20: str, class10: str | None = None) -> list[dict[str, Any]]:
    """警報 JSON から、対象市町村で発表中の警報・注意報を取り出す。

    新形式（r8）: 報のリスト。種別（dataTypeCode）ごとに最新の報だけが現状。
    旧形式: {"areaTypes": [{"areas": [{"code", "warnings": [...]}]}]}
    """
    found: dict[str, dict[str, Any]] = {}

    def _add(kinds: list, area_code: str) -> None:
        for k in kinds or []:
            code, status = k.get("code"), k.get("status", "")
            if code in (None, "") or status not in ACTIVE_STATUSES:
                continue
            key = str(int(str(code))) if str(code).isdigit() else str(code)
            found.setdefault(
                key,
                {
                    "code": key,
                    "name": warning_name(code),
                    "status": status,
                    "area_code": area_code,
                    "mountain_relevant": key in MOUNTAIN_RELEVANT_WARNINGS,
                },
            )

    if isinstance(data, list):
        latest: dict[str, dict] = {}
        for rep in data:
            if not isinstance(rep, dict):
                continue
            t = rep.get("dataTypeCode")
            if t and (t not in latest or rep.get("reportDatetime", "") > latest[t].get("reportDatetime", "")):
                latest[t] = rep
        for rep in latest.values():
            w = rep.get("warning") or {}
            for it in w.get("class20Items") or []:
                if it.get("areaCode") == class20:
                    _add(it.get("kinds"), class20)
            for it in w.get("class10Items") or []:
                if class10 and it.get("areaCode") == class10:
                    _add(it.get("kinds"), class10)
    elif isinstance(data, dict):
        for at in data.get("areaTypes") or []:
            for a in at.get("areas") or []:
                if a.get("code") in (class20, class10):
                    _add(a.get("warnings"), a.get("code"))
    return sorted(found.values(), key=lambda w: int(w["code"]) if w["code"].isdigit() else 999)


def fetch_jma_warnings(office: str, class20: str | None, class10: str | None) -> dict[str, Any]:
    page = _jma_warning_page(class20, office)
    if not class20:
        return {"error": "市町村コードが不明なため警報を照合できません", "page": page}
    result: dict[str, Any] = {"page": page, "area_code": class20}
    try:
        with jp_geo.client() as c:
            resp = c.get(JMA_WARNING_URL.format(office=office))
            if resp.status_code == 404:
                # r8 がさらに改版された可能性。旧形式で読み、古いデータである旨を残す。
                resp = c.get(JMA_WARNING_LEGACY_URL.format(office=office))
                result["stale_warning"] = (
                    "新形式（data/r8/）が見つからず旧パスで読んだため、内容が古い可能性があります。"
                    "必ず気象庁の警報ページで確認してください。"
                )
            resp.raise_for_status()
            data = resp.json()
            try:
                mt = c.get(JMA_WARNING_MAP_TIME_URL)
                if mt.status_code == 200:
                    latest = (mt.json() or {}).get("latestControlDatetime")
                    result["system_updated"] = latest
                    if latest:
                        age = datetime.now().astimezone() - datetime.fromisoformat(latest)
                        if age > timedelta(hours=6):
                            result["stale_warning"] = (
                                f"気象庁の警報データが{age.total_seconds() / 3600:.0f}時間更新されていません。"
                                "公式ページで確認してください。"
                            )
            except Exception:  # noqa: BLE001 — 鮮度確認は任意
                pass
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "page": page}
    result["active"] = parse_warnings(data, class20, class10)
    result["note"] = "JSON は気象庁の非公開仕様の内部データ。判断は必ず公式ページの表示で行うこと。"
    return result


# ---------------------------------------------------------------- 日の出・日の入り


def fetch_daylight(lat: float, lon: float, date_str: str) -> dict[str, Any]:
    try:
        import zoneinfo

        from astral import LocationInfo
        from astral.sun import dawn, dusk, sun

        tz = zoneinfo.ZoneInfo(TIMEZONE)
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        obs = LocationInfo(latitude=lat, longitude=lon).observer
        s = sun(obs, date=d, tzinfo=tz)
        fmt = "%H:%M"

        def _tw(fn, depression):
            try:
                return fn(obs, date=d, depression=depression, tzinfo=tz).strftime(fmt)
            except Exception:  # noqa: BLE001
                return None

        return {
            "date": date_str,
            "astronomical_dawn": _tw(dawn, 18),
            "nautical_dawn": _tw(dawn, 12),
            "civil_dawn": s["dawn"].strftime(fmt),
            "sunrise": s["sunrise"].strftime(fmt),
            "sunset": s["sunset"].strftime(fmt),
            "civil_dusk": s["dusk"].strftime(fmt),
            "nautical_dusk": _tw(dusk, 12),
            "astronomical_dusk": _tw(dusk, 18),
            "daylight_hours": round((s["sunset"] - s["sunrise"]).total_seconds() / 3600, 1),
            "timezone": TIMEZONE,
            "note": "海抜0m・平坦な地平線での計算値。谷や東斜面では実際の日照はもっと短い。",
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ---------------------------------------------------------------- 地理・施設（OSM / 地理院）


def fetch_volcanoes(lat: float, lon: float, radius_km: float = 15) -> dict[str, Any]:
    q = f"""
[out:json][timeout:30];
node["natural"="volcano"](around:{radius_km * 1000:.0f},{lat},{lon});
out tags;
"""
    try:
        els = jp_geo.overpass(q)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "links": _volcano_links()}
    volcanoes = [p for p in (jp_geo.osm_place(e, lat, lon, "名称不明の火山") for e in els) if p]
    volcanoes.sort(key=lambda v: v["distance_km"])
    return {
        "volcanoes": volcanoes,
        "is_volcanic_area": bool(volcanoes),
        "links": _volcano_links(),
        "note": (
            "OSM の natural=volcano による近接判定。該当があれば気象庁の噴火警戒レベル・"
            "火山の状況に関する解説情報を必ず確認すること（この判定は活動度を示さない）。"
        ),
    }


def _volcano_links() -> dict[str, str]:
    return {
        "jma_volcano_for_climbers": "https://www.data.jma.go.jp/vois/data/tokyo/STOCK/activity_info/index.html",
        "jma_volcano_map": "https://www.jma.go.jp/bosai/map.html#5/34.5/137/&contents=volcano",
    }


def fetch_municipalities(
    summit: tuple[float, float], trailhead: tuple[float, float] | None, n: int = 12
) -> dict[str, Any]:
    points = jp_geo.sample_line(trailhead, summit, n) if trailhead else [summit]
    seen: dict[str, dict[str, Any]] = {}
    summit_info: dict[str, Any] = {}
    fails = 0
    for i, (la, lo) in enumerate(points):
        info = jp_geo.locate(la, lo)
        if "error" in info:
            fails += 1
            if fails >= 4:
                break
            continue
        if i == len(points) - 1:
            summit_info = info
        code = info["muni_code"]
        if code not in seen:
            seen[code] = {
                "prefecture": info.get("prefecture"),
                "municipality": info.get("municipality"),
                "muni_code": code,
            }
    if not seen:
        return {"error": "市町村を特定できませんでした", "municipalities": []}
    out: dict[str, Any] = {"municipalities": list(seen.values()), "summit": summit_info}
    if not trailhead:
        out["note"] = "山頂の市町村のみ。--trailhead を渡すと登山口→山頂の通過市町村を調べる。"
    return out


def fetch_hospitals(lat: float, lon: float) -> dict[str, Any]:
    q = f"""
[out:json][timeout:30];
nwr["amenity"="hospital"](around:40000,{lat},{lon});
out center tags;
"""
    try:
        els = jp_geo.overpass(q)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    hs = []
    for e in els:
        h = jp_geo.osm_place(e, lat, lon, "名称不明の病院")
        if h:
            h["emergency"] = (e.get("tags") or {}).get("emergency") == "yes"
            hs.append(h)
    hs.sort(key=lambda h: (not h.get("emergency"), h["distance_km"]))
    return {
        "hospitals": hs[:3],
        "note": "OSM データ。救急告示病院かどうかは OSM に入っていないことが多いので、各病院の公式情報で確認。",
    }


def fetch_police(lat: float, lon: float) -> dict[str, Any]:
    q = f"""
[out:json][timeout:30];
nwr["amenity"="police"](around:30000,{lat},{lon});
out center tags;
"""
    try:
        els = jp_geo.overpass(q)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    ps = [p for p in (jp_geo.osm_place(e, lat, lon, "警察施設") for e in els) if p]
    # 警察署を交番・駐在所より優先
    ps.sort(key=lambda p: ("警察署" not in p["name"], p["distance_km"]))
    return {
        "stations": ps[:3],
        "note": "山岳遭難は 110番（警察）または 119番（消防）。登山届は地元警察署・コンパスで提出。",
    }


def fetch_huts(lat: float, lon: float, radius_km: float = 8) -> dict[str, Any]:
    q = f"""
[out:json][timeout:30];
nwr["tourism"~"^(alpine_hut|wilderness_hut)$"](around:{radius_km * 1000:.0f},{lat},{lon});
out center tags;
"""
    try:
        els = jp_geo.overpass(q)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    huts = []
    for e in els:
        p = jp_geo.osm_place(e, lat, lon, "名称不明の小屋")
        if p:
            p["type"] = "避難小屋" if e.get("tags", {}).get("tourism") == "wilderness_hut" else "山小屋"
            huts.append(p)
    huts.sort(key=lambda h: h["distance_km"])
    return {
        "huts": huts[:12],
        "note": "OSM データ。営業期間・予約の要否・テント場の有無は各山小屋の公式サイトで確認。",
    }


def fetch_campgrounds(lat: float, lon: float, radius_km: float = 15) -> dict[str, Any]:
    q = f"""
[out:json][timeout:30];
nwr["tourism"="camp_site"](around:{radius_km * 1000:.0f},{lat},{lon});
out center tags;
"""
    try:
        els = jp_geo.overpass(q)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    camps = [p for p in (jp_geo.osm_place(e, lat, lon, "名称不明のキャンプ場") for e in els) if p]
    camps.sort(key=lambda c: c["distance_km"])
    return {
        "campgrounds": camps[:10],
        "note": "OSM の tourism=camp_site（山中のテント場を含むことがある）。指定地以外での幕営は原則不可の山域が多い。",
    }


def fetch_protected_areas(lat: float, lon: float) -> dict[str, Any]:
    q = f"""
[out:json][timeout:30];
is_in({lat},{lon})->.a;
(
  area.a["boundary"="national_park"];
  area.a["boundary"="protected_area"];
  area.a["leisure"="nature_reserve"];
);
out tags;
"""
    try:
        els = jp_geo.overpass(q)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    names = []
    for e in els:
        t = e.get("tags", {})
        n = t.get("name:ja") or t.get("name")
        if n and n not in names:
            names.append(n)
    return {
        "areas": names,
        "note": "国立公園・国定公園・自然公園などの規制（植物採取・幕営・ドローン等）は環境省・都道府県で確認。",
    }


# ---------------------------------------------------------------- 時間・行程の計算


def _din_hours(distance_km: float, up_m: float, down_m: float) -> float:
    """DIN 33466 の歩行時間（水平 4km/h、登り 300m/h、下り 500m/h）。

    日本のガイドブックのコースタイムに近い値になる。
    """
    th = distance_km / 4.0
    tv = up_m / 300.0 + down_m / 500.0
    return max(th, tv) + min(th, tv) / 2


def estimate_times(
    distance_km: float,
    up_m: float,
    down_m: float | None = None,
    body_kg: float | None = None,
    pack_kg: float | None = None,
) -> dict[str, Any]:
    down_m = up_m if down_m is None else down_m
    standard = _din_hours(distance_km, up_m, down_m)
    # 山本正嘉のコース定数 = 1.8×時間(h) + 0.3×距離(km) + 10.0×登り(km) + 0.6×下り(km)
    course_constant = 1.8 * standard + 0.3 * distance_km + 10.0 * up_m / 1000 + 0.6 * down_m / 1000
    out: dict[str, Any] = {
        "fast_hr": round(standard * 0.8, 1),
        "standard_hr": round(standard, 1),
        "leisurely_hr": round(standard * 1.3, 1),
        "course_constant": round(course_constant, 1),
        "course_constant_guide": _course_constant_guide(course_constant),
        "note": (
            "休憩を含まない歩行時間。DIN 33466（水平4km/h・登り300m/h・下り500m/h）で概算。"
            "山と高原地図・ヤマレコ・YAMAP のコースタイムがあればそちらを優先すること。"
        ),
    }
    if body_kg:
        load = body_kg + (pack_kg or 0)
        out["energy_kcal"] = round(course_constant * load)
        # 脱水量の目安(ml) = 体重(kg) × 行動時間(h) × 5
        out["dehydration_ml"] = round(body_kg * standard * 5, -1)
        out["energy_note"] = "エネルギー ≒ コース定数 ×（体重＋荷物）。脱水量 ≒ 体重×時間×5。その7〜8割を行動中に補給する。"
    return out


def _course_constant_guide(cc: float) -> str:
    if cc < 20:
        return "20未満: 体力的には比較的やさしい"
    if cc < 30:
        return "20〜30: 一般的な日帰り登山"
    if cc < 40:
        return "30〜40: きつめの日帰り（体力に自信がない人は山小屋泊も検討）"
    return "40以上: 健脚向け。山小屋泊・テント泊の行程を推奨"


def build_itinerary(
    start_time: str,
    distance_km: float,
    up_m: float,
    down_m: float | None,
    daylight: dict[str, Any],
    finish_by: str = "15:00",
) -> dict[str, Any]:
    """出発時刻から山頂到着・下山時刻を見積もる。

    日本の山の原則「早出早着」（山小屋・登山口には 15時ごろまでに着く）と、
    日没に間に合うかを判定する。往路と復路は距離を半分ずつとみなす。
    """
    down_m = up_m if down_m is None else down_m
    try:
        start = datetime.strptime(start_time, "%H:%M")
        finish_limit = datetime.strptime(finish_by, "%H:%M")
    except ValueError:
        return {"error": f"時刻の形式が不正です: {start_time!r} / {finish_by!r}（HH:MM）"}

    ascent_hr = _din_hours(distance_km / 2, up_m, 0)
    descent_hr = _din_hours(distance_km / 2, 0, down_m)
    summit = start + timedelta(hours=ascent_hr)
    back = summit + timedelta(hours=descent_hr)

    def _fmt(dt: datetime) -> str:
        s = dt.strftime("%H:%M")
        return s + "（翌日）" if dt.date() > start.date() else s

    sunset_str = daylight.get("sunset")
    after_dark = False
    turnaround = None
    if sunset_str:
        sunset = datetime.strptime(sunset_str, "%H:%M")
        after_dark = back > sunset
        turnaround = sunset - timedelta(hours=descent_hr)
    return {
        "start_time": start_time,
        "summit_eta": _fmt(summit),
        "return_eta": _fmt(back),
        "turnaround_by": _fmt(turnaround) if turnaround else None,
        "total_hr": round(ascent_hr + descent_hr, 1),
        "late_arrival": back > finish_limit,
        "finish_by": finish_by,
        "after_dark": after_dark,
        "sunset": sunset_str,
        "note": (
            "休憩を含まない標準ペース。turnaround_by は「この時刻までに下山を開始しないと日没までに戻れない」時刻。"
            "実際は休憩・渋滞（鎖場・梯子）を見込んで余裕を持つこと。"
        ),
    }


def compute_bearings(waypoints: list[tuple[float, float]]) -> dict[str, Any]:
    segs = []
    total = 0.0
    for i in range(len(waypoints) - 1):
        (la1, lo1), (la2, lo2) = waypoints[i], waypoints[i + 1]
        p1, p2 = math.radians(la1), math.radians(la2)
        dl = math.radians(lo2 - lo1)
        x = math.sin(dl) * math.cos(p2)
        y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
        brg = (math.degrees(math.atan2(x, y)) + 360) % 360
        d = jp_geo.haversine_km(la1, lo1, la2, lo2)
        total += d
        segs.append(
            {
                "from": i,
                "to": i + 1,
                "bearing_deg": round(brg, 1),
                "distance_km": round(d, 2),
                "cumulative_km": round(total, 2),
            }
        )
    return {
        "segments": segs,
        "total_km": round(total, 2),
        "note": "真北基準。日本の磁気偏角は西偏およそ5〜10°（地理院地図の「磁北線」で確認）。",
    }


# ---------------------------------------------------------------- CLI


def _parse_latlon(s: str, opt: str) -> tuple[float, float]:
    try:
        la, lo = map(float, s.split(","))
        return la, lo
    except ValueError:
        click.echo(json.dumps({"error": f"{opt} の形式が不正です。lat,lon で指定"}, ensure_ascii=False), err=True)
        sys.exit(1)


@click.command()
@click.option("--coordinates", required=True, help="山頂の座標 lat,lon")
@click.option("--elevation", required=True, type=float, help="山頂標高（m）")
@click.option("--peak-name", required=True, help="山名")
@click.option("--date", default=None, help="日の出・日の入りを計算する日 YYYY-MM-DD（既定: 今日）")
@click.option("--trailhead", default=None, help="登山口の座標 lat,lon（通過市町村・キャンプ場検索に使う）")
@click.option("--distance-km", type=float, default=None, help="往復の距離（km）")
@click.option("--up-m", type=float, default=None, help="累積標高差 登り（m）")
@click.option("--down-m", type=float, default=None, help="累積標高差 下り（m、省略時は登りと同じ）")
@click.option("--start-time", default=None, help="出発時刻 HH:MM（行程表を作る）")
@click.option("--finish-by", default="15:00", show_default=True, help="下山・小屋到着の目標時刻")
@click.option("--body-kg", type=float, default=None, help="体重（kg、エネルギー・水分の目安用）")
@click.option("--pack-kg", type=float, default=None, help="荷物の重さ（kg）")
@click.option("--waypoint", "waypoints", multiple=True, help="通過点 lat,lon（2つ以上で方位を計算）")
def cli(
    coordinates: str,
    elevation: float,
    peak_name: str,
    date: str | None,
    trailhead: str | None,
    distance_km: float | None,
    up_m: float | None,
    down_m: float | None,
    start_time: str | None,
    finish_by: str,
    body_kg: float | None,
    pack_kg: float | None,
    waypoints: tuple[str, ...],
) -> None:
    """山頂座標からコンディション情報をまとめて取得し JSON で出力する。"""
    lat, lon = _parse_latlon(coordinates, "--coordinates")
    th = _parse_latlon(trailhead, "--trailhead") if trailhead else None
    date_str = date or datetime.now().strftime("%Y-%m-%d")
    gaps: list[dict[str, str]] = []

    def _gap(name: str, res: dict) -> dict:
        if isinstance(res, dict) and "error" in res:
            gaps.append({"source": name, "reason": str(res["error"])})
        return res

    weather = _gap("Open-Meteo 天気", fetch_weather(lat, lon, elevation))
    if "error" not in weather:
        enrich_forecast(weather["forecast"], elevation)

    municipalities = _gap("通過市町村（地理院・気象庁地域コード）", fetch_municipalities((lat, lon), th))
    jma_area = municipalities.get("summit") or {}
    office = jma_area.get("office_code")
    if office:
        jma_forecast = _gap("気象庁 府県天気予報", fetch_jma_forecast(office, jma_area.get("class10_code")))
        jma_warnings = _gap(
            "気象庁 警報・注意報",
            fetch_jma_warnings(office, jma_area.get("class20_code"), jma_area.get("class10_code")),
        )
    else:
        jma_forecast = _gap("気象庁 府県天気予報", {"error": "府県予報区を特定できませんでした"})
        jma_warnings = _gap("気象庁 警報・注意報", {"error": "府県予報区を特定できませんでした"})

    base = th or (lat, lon)
    output: dict[str, Any] = {
        "peak": {
            "name": peak_name,
            "lat": lat,
            "lon": lon,
            "elevation_m": elevation,
            "links": jp_geo.map_links(lat, lon),
        },
        "location": jma_area,
        "weather": weather,
        "jma_forecast": jma_forecast,
        "jma_warnings": jma_warnings,
        "air_quality": _gap("Open-Meteo 大気質", fetch_air_quality(lat, lon)),
        "daylight": _gap("日の出・日の入り計算", fetch_daylight(lat, lon, date_str)),
        "volcano": _gap("近接火山（OSM）", fetch_volcanoes(lat, lon)),
        "municipalities": municipalities,
        "protected_areas": _gap("国立公園等（OSM）", fetch_protected_areas(lat, lon)),
        "nearest_hospital": _gap("病院（OSM）", fetch_hospitals(*base)),
        "police": _gap("警察（OSM）", fetch_police(*base)),
        "huts": _gap("山小屋（OSM）", fetch_huts(lat, lon)),
        "campgrounds": _gap("キャンプ場（OSM）", fetch_campgrounds(*base)),
        "gaps": gaps,
    }

    if distance_km is not None and up_m is not None:
        output["time_estimates"] = estimate_times(distance_km, up_m, down_m, body_kg, pack_kg)
        if start_time:
            output["itinerary"] = build_itinerary(
                start_time, distance_km, up_m, down_m, output["daylight"], finish_by
            )
    if len(waypoints) >= 2:
        output["bearings"] = compute_bearings([_parse_latlon(w, "--waypoint") for w in waypoints])

    click.echo(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()

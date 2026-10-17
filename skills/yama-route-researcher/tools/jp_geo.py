#!/usr/bin/env python3
"""日本の山域向けの共通ヘルパー（国土地理院・気象庁・OpenStreetMap）。

find_peak.py と fetch_conditions.py の両方から使う。
すべての関数は失敗時に例外を投げるか、{"error": ...} を返す。呼び出し側で
グレースフルに劣化させる（スキル全体を止めない）方針は元プロジェクトと同じ。
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import httpx

USER_AGENT = (
    "yama-route-researcher/0.1 "
    "(Japanese adaptation of https://github.com/dreamiurg/claude-mountaineering-skills)"
)

GSI_ADDRESS_SEARCH_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
GSI_REVERSE_GEOCODER_URL = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress"
GSI_ELEVATION_URL = "https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php"
JMA_AREA_URL = "https://www.jma.go.jp/bosai/common/const/area.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# 本家が 502/504/429 を返すことが多いため、順にミラーを試す。
OVERPASS_URLS = (
    OVERPASS_URL,
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_RETRY_WAIT_S = 5.0
_overpass_slots = threading.BoundedSemaphore(2)

# 日本全体をおおまかに覆う bbox（south, west, north, east）
JAPAN_BBOX = (20.0, 122.0, 46.0, 154.0)

PREFECTURES = {
    "01": "北海道", "02": "青森県", "03": "岩手県", "04": "宮城県", "05": "秋田県",
    "06": "山形県", "07": "福島県", "08": "茨城県", "09": "栃木県", "10": "群馬県",
    "11": "埼玉県", "12": "千葉県", "13": "東京都", "14": "神奈川県", "15": "新潟県",
    "16": "富山県", "17": "石川県", "18": "福井県", "19": "山梨県", "20": "長野県",
    "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県", "25": "滋賀県",
    "26": "京都府", "27": "大阪府", "28": "兵庫県", "29": "奈良県", "30": "和歌山県",
    "31": "鳥取県", "32": "島根県", "33": "岡山県", "34": "広島県", "35": "山口県",
    "36": "徳島県", "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県",
    "41": "佐賀県", "42": "長崎県", "43": "熊本県", "44": "大分県", "45": "宮崎県",
    "46": "鹿児島県", "47": "沖縄県",
}

# area.json が取れないときの都府県 → 気象庁府県予報区コード。
# 北海道・鹿児島・沖縄は複数の予報区に分かれるため代表値（要確認）。
PREF_TO_JMA_OFFICE_FALLBACK = {
    "01": "016000", "46": "460100", "47": "471000",
    **{p: f"{p}0000" for p in PREFECTURES if p not in ("01", "46", "47")},
}

_area_cache: dict[str, Any] = {}


def client(timeout: float = 30.0) -> httpx.Client:
    """共通の httpx クライアント。テストではこの関数を差し替える。"""
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})


# ---------------------------------------------------------------- 計算ヘルパー


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def sample_line(
    start: tuple[float, float], end: tuple[float, float], n: int
) -> list[tuple[float, float]]:
    if n <= 1:
        return [start]
    return [
        (start[0] + (end[0] - start[0]) * i / (n - 1), start[1] + (end[1] - start[1]) * i / (n - 1))
        for i in range(n)
    ]


def map_links(lat: float, lon: float, zoom: int = 15) -> dict[str, str]:
    """山頂・登山口などの地図リンク一式。"""
    return {
        "gsi": f"https://maps.gsi.go.jp/#{zoom}/{lat:.6f}/{lon:.6f}/",
        "google": f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}",
        "peakvisor": (
            f"https://peakvisor.com/hiking-map?custom={zoom}%2F{lat:.5f}%2F{lon:.5f}"
            f"#{zoom}/{lat:.5f}/{lon:.5f}"
        ),
        "windy": f"https://www.windy.com/?{lat:.3f},{lon:.3f},10",
    }


# ---------------------------------------------------------------- 国土地理院


def gsi_address_search(query: str) -> list[dict[str, Any]]:
    """地理院の地名検索。山名・地名から座標候補を返す。"""
    with client() as c:
        resp = c.get(GSI_ADDRESS_SEARCH_URL, params={"q": query})
        resp.raise_for_status()
        out = []
        for feat in resp.json() or []:
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            out.append(
                {
                    "name": (feat.get("properties") or {}).get("title", ""),
                    "lat": float(coords[1]),
                    "lon": float(coords[0]),
                    "source": "国土地理院 地名検索",
                }
            )
        return out


def gsi_reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """座標 → 市区町村コード（5桁）と町字名。"""
    with client() as c:
        resp = c.get(GSI_REVERSE_GEOCODER_URL, params={"lat": lat, "lon": lon})
        resp.raise_for_status()
        results = (resp.json() or {}).get("results") or {}
        muni = str(results.get("muniCd") or "")
        if not muni:
            return {"error": "地理院逆ジオコーダが市区町村を返しませんでした（海上・県境付近の可能性）"}
        return {"muni_code": muni.zfill(5), "district": results.get("lv01Nm")}


def gsi_elevation(lat: float, lon: float) -> float | None:
    """地理院 DEM による標高（m）。取得できなければ None。"""
    with client() as c:
        resp = c.get(GSI_ELEVATION_URL, params={"lat": lat, "lon": lon, "outtype": "JSON"})
        resp.raise_for_status()
        elev = (resp.json() or {}).get("elevation")
        try:
            return round(float(elev), 1)
        except (TypeError, ValueError):
            return None  # "-----" などデータ無し


# ---------------------------------------------------------------- 気象庁 地域コード


def jma_area_master() -> dict[str, Any]:
    if "area" not in _area_cache:
        with client() as c:
            resp = c.get(JMA_AREA_URL)
            resp.raise_for_status()
            _area_cache["area"] = resp.json()
    return _area_cache["area"]


def resolve_jma_area(muni_code: str) -> dict[str, Any]:
    """市区町村コード → 気象庁の class20（市町村）/ class10（一次細分）/ office（府県予報区）。

    気象庁の class20 コードは原則「市区町村コード5桁 + 00」。政令市などで分割されている
    場合は同じ5桁で始まる最初のコードを採用する。
    """
    pref = muni_code[:2]
    result: dict[str, Any] = {"prefecture": PREFECTURES.get(pref)}
    try:
        area = jma_area_master()
    except Exception as e:  # noqa: BLE001 — 地域マスターが取れなくても都府県単位で続行
        result["office_code"] = PREF_TO_JMA_OFFICE_FALLBACK.get(pref)
        result["note"] = f"気象庁地域マスター取得失敗のため都府県単位で代用: {e}"
        return result

    class20s = area.get("class20s", {})
    code20 = muni_code + "00"
    if code20 not in class20s:
        code20 = next((k for k in class20s if k.startswith(muni_code)), None)
    if not code20:
        result["office_code"] = PREF_TO_JMA_OFFICE_FALLBACK.get(pref)
        result["note"] = "市区町村が気象庁地域マスターに見つからないため都府県単位で代用"
        return result

    c20 = class20s[code20]
    # 松本市（松本／乗鞍上高地）のように市町村が気象庁の区域で分割されている場合、
    # 山頂がどちらの区域か座標からは決められないので候補をすべて返す。
    alternatives = [
        {"code": k, "name": v.get("name")} for k, v in class20s.items() if k.startswith(muni_code)
    ]
    code15 = c20.get("parent")
    code10 = (area.get("class15s", {}).get(code15) or {}).get("parent")
    office = (area.get("class10s", {}).get(code10) or {}).get("parent")
    result.update(
        {
            "municipality": c20.get("name"),
            "class20_code": code20,
            "class10_code": code10,
            "class10_name": (area.get("class10s", {}).get(code10) or {}).get("name"),
            "office_code": office,
            "office_name": (area.get("offices", {}).get(office) or {}).get("name"),
        }
    )
    if len(alternatives) > 1:
        result["class20_alternatives"] = alternatives
        result["class20_note"] = (
            "この市町村は気象庁の警報区域が分割されている。jma_warnings は全区域分を返すので、"
            "山域に当たる区域（例: 松本市乗鞍上高地）の警報を確認すること。"
        )
    return result


def locate(lat: float, lon: float) -> dict[str, Any]:
    """座標 → 都道府県・市町村・気象庁地域コードをまとめて返す。"""
    try:
        rg = gsi_reverse_geocode(lat, lon)
    except Exception as e:  # noqa: BLE001
        return {"error": f"地理院逆ジオコーダ失敗: {e}"}
    if "error" in rg:
        return rg
    info = resolve_jma_area(rg["muni_code"])
    info["muni_code"] = rg["muni_code"]
    info["district"] = rg.get("district")
    return info


# ---------------------------------------------------------------- OpenStreetMap


def overpass(query: str, timeout: float = 40.0) -> list[dict[str, Any]]:
    """Overpass API に問い合わせる。サーバー障害・混雑時は次のミラーを試す。"""
    errors: list[str] = []
    # overpass-api.de は同時2接続まで。超えると 429 になるので並列呼び出しを絞る。
    with _overpass_slots, client(timeout=timeout) as c:
        for url in OVERPASS_URLS:
            for attempt in range(2):
                try:
                    resp = c.post(url, data={"data": query})
                    if resp.status_code == 429 and attempt == 0:
                        time.sleep(OVERPASS_RETRY_WAIT_S)
                        continue
                    resp.raise_for_status()
                    return resp.json().get("elements", [])
                except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as e:
                    errors.append(f"{httpx.URL(url).host}: {str(e).splitlines()[0]}")
                    break
    raise RuntimeError("Overpass のすべてのサーバーが失敗: " + " / ".join(errors))


def osm_coords(el: dict) -> tuple[float | None, float | None]:
    lat = el["lat"] if "lat" in el else (el.get("center") or {}).get("lat")
    lon = el["lon"] if "lon" in el else (el.get("center") or {}).get("lon")
    return lat, lon


def osm_address(tags: dict) -> str | None:
    """OSM の addr:* タグから日本式の住所文字列を組み立てる。"""
    if tags.get("addr:full"):
        return tags["addr:full"]
    parts = [
        tags.get("addr:postcode") and f"〒{tags['addr:postcode']}",
        tags.get("addr:province"),
        tags.get("addr:county"),
        tags.get("addr:city"),
        tags.get("addr:suburb"),
        tags.get("addr:quarter"),
        tags.get("addr:neighbourhood"),
        tags.get("addr:street"),
        "-".join(p for p in (tags.get("addr:block_number"), tags.get("addr:housenumber")) if p),
    ]
    joined = " ".join(p for p in parts if p)
    return joined or None


def osm_place(el: dict, ref_lat: float, ref_lon: float, default_name: str) -> dict | None:
    tags = el.get("tags", {})
    lat, lon = osm_coords(el)
    if lat is None or lon is None:
        return None
    entry: dict[str, Any] = {
        "name": tags.get("name:ja") or tags.get("name") or default_name,
        "lat": lat,
        "lon": lon,
        "distance_km": round(haversine_km(ref_lat, ref_lon, lat, lon), 1),
    }
    phone = tags.get("phone") or tags.get("contact:phone")
    website = tags.get("website") or tags.get("contact:website") or tags.get("url")
    address = osm_address(tags)
    if phone:
        entry["phone"] = phone
    if website:
        entry["website"] = website
    if address:
        entry["address"] = address
    if tags.get("ele"):
        entry["ele_m"] = tags["ele"]
    return entry

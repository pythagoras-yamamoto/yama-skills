"""オフラインで動く単体テスト（HTTP はすべてモック）。"""

import json

import httpx
import pytest
from click.testing import CliRunner

import fetch_conditions as fc
import find_peak as fp
import jp_geo

AREA = {
    "offices": {"200000": {"name": "長野県"}},
    "class10s": {"200020": {"name": "中部", "parent": "200000"}},
    "class15s": {"200021": {"name": "松本地域", "parent": "200020"}},
    "class20s": {"2020200": {"name": "松本市", "parent": "200021"}},
}

R8_WARNINGS = [
    {
        "dataTypeCode": "VPWW61",
        "reportDatetime": "2026-10-17T05:00:00+09:00",
        "warning": {"class20Items": [{"areaCode": "2020200", "kinds": [
            {"code": "14", "status": "発表"}, {"code": "20", "status": "解除"}]}]},
    },
    {   # 古い報は無視される
        "dataTypeCode": "VPWW61",
        "reportDatetime": "2026-10-16T05:00:00+09:00",
        "warning": {"class20Items": [{"areaCode": "2020200", "kinds": [
            {"code": "22", "status": "発表"}]}]},
    },
    {
        "dataTypeCode": "VPWW58",
        "reportDatetime": "2026-10-17T06:00:00+09:00",
        "warning": {"class20Items": [{"areaCode": "9999900", "kinds": [
            {"code": "05", "status": "発表"}]}]},
    },
]


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "LonLatToAddress" in url:
        return httpx.Response(200, json={"results": {"muniCd": "20202", "lv01Nm": "安曇"}})
    if "AddressSearch" in url:
        return httpx.Response(200, json=[
            {"geometry": {"coordinates": [137.647639, 36.341944]}, "properties": {"title": "槍ヶ岳"}},
            {"geometry": {"coordinates": [139.0, 35.0]}, "properties": {"title": "槍ヶ岳町"}},
        ])
    if "getelevation" in url:
        return httpx.Response(200, json={"elevation": 3180.2})
    if url.endswith("area.json"):
        return httpx.Response(200, json=AREA)
    if "overpass" in url:
        body = request.content.decode()
        if "peak" in body:
            return httpx.Response(200, json={"elements": [
                {"type": "node", "id": 1, "lat": 36.3420, "lon": 137.6477,
                 "tags": {"natural": "peak", "name": "槍ヶ岳", "ele": "3180", "wikipedia": "ja:槍ヶ岳"}}]})
        if "hospital" in body:
            return httpx.Response(200, json={"elements": [
                {"type": "node", "id": 2, "lat": 36.24, "lon": 137.97,
                 "tags": {"amenity": "hospital", "name": "テスト病院", "phone": "0263-00-0000",
                          "addr:province": "長野県", "addr:city": "松本市"}}]})
        return httpx.Response(200, json={"elements": []})
    if "forecast/data/forecast" in url:
        return httpx.Response(200, json=[{
            "publishingOffice": "長野地方気象台", "reportDatetime": "2026-10-17T05:00:00+09:00",
            "timeSeries": [
                {"timeDefines": ["2026-10-17T05:00:00+09:00"],
                 "areas": [{"area": {"name": "中部", "code": "200020"}, "weathers": ["晴れ"], "winds": ["北の風"]}]},
                {"timeDefines": ["2026-10-17T06:00:00+09:00"],
                 "areas": [{"area": {"name": "中部", "code": "200020"}, "pops": ["10"]}]},
            ]}])
    if "warning/data/r8/map_time" in url:
        return httpx.Response(200, json={"latestControlDatetime": "2000-01-01T00:00:00+09:00"})
    if "warning/data/r8" in url:
        return httpx.Response(200, json=R8_WARNINGS)
    if "air-quality" in url:
        return httpx.Response(200, json={"hourly": {
            "time": ["2026-10-17T00:00", "2026-10-17T01:00"], "pm2_5": [80, 90], "dust": [5, 12]}})
    if "api.open-meteo.com" in url:
        return httpx.Response(200, json={
            "timezone": "Asia/Tokyo",
            "daily": {"time": ["2026-10-17"], "weather_code": [0], "temperature_2m_max": [3.0],
                      "temperature_2m_min": [-4.0], "precipitation_sum": [0.0],
                      "precipitation_probability_max": [5], "wind_speed_10m_max": [8.0],
                      "wind_gusts_10m_max": [14.0]},
            "hourly": {"freezing_level_height": [2800] * 24, "wind_speed_700hPa": [16.0] * 24},
        })
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def mock_http(monkeypatch):
    jp_geo._area_cache.clear()
    monkeypatch.setattr(jp_geo, "client", lambda timeout=30.0: httpx.Client(transport=httpx.MockTransport(_handler)))


def test_name_variants():
    v = fp.name_variants("槍ヶ岳")
    assert v[0] == "槍ヶ岳" and "槍ケ岳" in v and "槍が岳" in v and "槍ヶ嶽" in v


def test_find_peak_merges_and_enriches():
    out = fp.find_peaks("槍ヶ岳")
    assert len(out["candidates"]) == 1
    c = out["candidates"][0]
    assert c["source"] == "OpenStreetMap" and c["also_found_in"] == ["国土地理院 地名検索"]
    assert c["prefecture"] == "長野県" and c["municipality"] == "松本市"
    assert c["ele_m"] == 3180.0
    assert c["links"]["wikipedia"].endswith("/槍ヶ岳")
    assert c["links"]["gsi"].startswith("https://maps.gsi.go.jp/#15/36.342")


def test_resolve_jma_area():
    a = jp_geo.resolve_jma_area("20202")
    assert a["class20_code"] == "2020200" and a["class10_code"] == "200020" and a["office_code"] == "200000"


def test_parse_warnings_r8_uses_latest_report_only():
    ws = fc.parse_warnings(R8_WARNINGS, "2020200", "200020")
    assert [w["name"] for w in ws] == ["雷注意報"]
    assert ws[0]["mountain_relevant"]


def test_parse_warnings_legacy_format():
    legacy = {"areaTypes": [{"areas": [{"code": "2020200", "warnings": [
        {"code": "22", "status": "継続"}, {"code": "14", "status": "解除"}]}]}]}
    assert [w["name"] for w in fc.parse_warnings(legacy, "2020200")] == ["なだれ注意報"]


def test_enrich_forecast_flags():
    days = [{"freezing_level_m": 2900, "wind_max_ms": 8, "upper_wind_max_ms": 16}]
    fc.enrich_forecast(days, 3180)
    assert days[0]["near_summit"] and "山頂は氷点下" in days[0]["snow_line_note"]
    assert days[0]["wind_alert"] == "稜線は危険（15m/s以上）"


def test_estimate_times_and_course_constant():
    t = fc.estimate_times(10, 1000, None, body_kg=60, pack_kg=8)
    # 水平 2.5h, 垂直 1000/300+1000/500 = 5.33h → 5.33 + 1.25 = 6.58h
    assert t["standard_hr"] == 6.6
    assert t["course_constant"] == pytest.approx(1.8 * 6.583 + 3 + 10 + 0.6, abs=0.1)
    assert t["energy_kcal"] == round(t["course_constant"] * 68, 0) or t["energy_kcal"] > 0


def test_itinerary_flags_late_and_dark():
    it = fc.build_itinerary("09:00", 10, 1000, None, {"sunset": "16:50"})
    assert it["late_arrival"] is True
    assert it["after_dark"] is False
    it2 = fc.build_itinerary("12:00", 10, 1000, None, {"sunset": "16:50"})
    assert it2["after_dark"] is True


def test_bearings_north():
    b = fc.compute_bearings([(36.0, 137.0), (36.1, 137.0)])
    assert b["segments"][0]["bearing_deg"] == 0.0
    assert b["total_km"] == pytest.approx(11.12, abs=0.05)


def test_cli_end_to_end():
    res = CliRunner().invoke(fc.cli, [
        "--coordinates", "36.3420,137.6477", "--elevation", "3180", "--peak-name", "槍ヶ岳",
        "--date", "2026-10-17", "--distance-km", "40", "--up-m", "2000", "--start-time", "05:00",
    ])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["location"]["office_code"] == "200000"
    assert out["jma_forecast"]["days"][0]["weather"] == "晴れ"
    assert [w["name"] for w in out["jma_warnings"]["active"]] == ["雷注意報"]
    assert "stale_warning" in out["jma_warnings"]
    assert out["weather"]["forecast"][0]["wind_alert"] == "稜線は危険（15m/s以上）"
    assert out["weather"]["forecast"][0]["feels_like_min_c"] == -20.0
    assert out["air_quality"]["daily"][0]["pm25_alert"] is True
    assert out["daylight"]["sunrise"].startswith("05:") or out["daylight"]["sunrise"].startswith("06:")
    assert out["nearest_hospital"]["hospitals"][0]["address"] == "長野県 松本市"
    assert "itinerary" in out and "time_estimates" in out

#!/usr/bin/env python3
"""山名から日本の山頂候補を探す（国土地理院 地名検索 + OpenStreetMap）。

日本には「駒ヶ岳」「丸山」のような同名の山が非常に多いため、候補をすべて
都道府県・標高つきで返し、どれを調べるかはスキル側でユーザーに確認する。

出力: JSON（stdout）。失敗しても exit 0 で {"candidates": [], "gaps": [...]} を返す。
"""

from __future__ import annotations

import json
from typing import Any

import click

import jp_geo

# 表記ゆれ（ヶ/ケ/が/ガ、岳/嶽 など）
_VARIANT_PAIRS = [("ヶ", "ケ"), ("ヶ", "が"), ("ヶ", "ガ"), ("ヶ", "ヵ"), ("岳", "嶽"), ("竜", "龍")]


def name_variants(name: str) -> list[str]:
    name = name.strip()
    variants = {name}
    for a, b in _VARIANT_PAIRS:
        for v in list(variants):
            if a in v:
                variants.add(v.replace(a, b))
            if b in v:
                variants.add(v.replace(b, a))
    return sorted(variants, key=lambda v: (v != name, v))


def _overpass_peaks(variants: list[str]) -> list[dict[str, Any]]:
    s, w, n, e = jp_geo.JAPAN_BBOX
    bbox = f"{s},{w},{n},{e}"
    names = "|".join(v.replace('"', "") for v in variants)
    query = f"""
[out:json][timeout:50];
(
  node["natural"~"^(peak|volcano)$"]["name"~"^({names})$"]({bbox});
  node["natural"~"^(peak|volcano)$"]["name:ja"~"^({names})$"]({bbox});
);
out tags;
"""
    out = []
    for el in jp_geo.overpass(query):
        tags = el.get("tags", {})
        out.append(
            {
                "name": tags.get("name:ja") or tags.get("name"),
                "lat": el.get("lat"),
                "lon": el.get("lon"),
                "ele_m": _to_float(tags.get("ele")),
                "natural": tags.get("natural"),
                "wikipedia": tags.get("wikipedia"),
                "osm_id": el.get("id"),
                "source": "OpenStreetMap",
            }
        )
    return out


def _to_float(v: Any) -> float | None:
    try:
        return float(str(v).replace("m", "").strip())
    except (TypeError, ValueError):
        return None


def _merge(cands: list[dict[str, Any]], radius_km: float = 1.0) -> list[dict[str, Any]]:
    """1 km 以内の候補は同一の山とみなして統合する（OSM の情報を優先）。"""
    merged: list[dict[str, Any]] = []
    for c in sorted(cands, key=lambda x: x["source"] != "OpenStreetMap"):
        if c.get("lat") is None or c.get("lon") is None:
            continue
        dup = next(
            (
                m
                for m in merged
                if jp_geo.haversine_km(m["lat"], m["lon"], c["lat"], c["lon"]) <= radius_km
            ),
            None,
        )
        if dup:
            sources = dup.setdefault("also_found_in", [])
            if c["source"] != dup["source"] and c["source"] not in sources:
                sources.append(c["source"])
        else:
            merged.append(dict(c))
    return merged


def _enrich(c: dict[str, Any]) -> None:
    loc = jp_geo.locate(c["lat"], c["lon"])
    if "error" not in loc:
        c["prefecture"] = loc.get("prefecture")
        c["municipality"] = loc.get("municipality")
    if c.get("ele_m") is None:
        try:
            c["ele_m"] = jp_geo.gsi_elevation(c["lat"], c["lon"])
            c["ele_source"] = "地理院DEM（地点の地表標高。三角点・山頂標高とは数m違うことがある）"
        except Exception:  # noqa: BLE001
            pass
    else:
        c["ele_source"] = "OSM ele タグ"
    c["links"] = jp_geo.map_links(c["lat"], c["lon"])
    if c.get("wikipedia", "").startswith("ja:"):
        c["links"]["wikipedia"] = "https://ja.wikipedia.org/wiki/" + c["wikipedia"][3:].replace(" ", "_")


def find_peaks(name: str, prefecture: str | None = None, limit: int = 10) -> dict[str, Any]:
    variants = name_variants(name)
    gaps: list[dict[str, str]] = []
    cands: list[dict[str, Any]] = []

    try:
        cands += _overpass_peaks(variants)
    except Exception as e:  # noqa: BLE001
        gaps.append({"source": "OpenStreetMap Overpass", "reason": str(e)})

    for v in variants:
        try:
            for r in jp_geo.gsi_address_search(v):
                # 地名検索は住所も返すので、山名と完全一致するものだけ残す
                if r["name"] in variants:
                    cands.append(r)
        except Exception as e:  # noqa: BLE001
            gaps.append({"source": f"国土地理院 地名検索（{v}）", "reason": str(e)})
            break

    merged = _merge(cands)
    for c in merged[: limit * 2]:
        _enrich(c)
    if prefecture:
        merged = [c for c in merged if prefecture in (c.get("prefecture") or "")] or merged
    merged.sort(key=lambda c: -(c.get("ele_m") or 0))

    return {
        "query": name,
        "variants_tried": variants,
        "candidates": merged[:limit],
        "gaps": gaps,
        "note": (
            "同名の山が複数ある場合は、都道府県・標高・地図リンクを示してユーザーに確認すること。"
            "見つからない場合は「〇〇山」「〇〇岳」の付け外し、旧称・別名（例: 御岳山/御嶽山）、"
            "ピーク名（例: 奥穂高岳）で再検索する。"
        ),
    }


@click.command()
@click.argument("name")
@click.option("--prefecture", default=None, help="都道府県名で絞り込み（例: 長野県）")
@click.option("--limit", default=10, show_default=True, help="返す候補の最大数")
def cli(name: str, prefecture: str | None, limit: int) -> None:
    """NAME（山名）に一致する山頂候補を JSON で出力する。"""
    try:
        out = find_peaks(name, prefecture, limit)
    except Exception as e:  # noqa: BLE001
        out = {"query": name, "candidates": [], "gaps": [{"source": "find_peak", "reason": str(e)}]}
    click.echo(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()

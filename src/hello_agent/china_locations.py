"""离线匹配中国省、市、区县，并提供受限的在线兜底。"""

import re
from functools import lru_cache
from typing import Any

import httpx
from pypinyin import Style, lazy_pinyin


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_CHINESE_PATTERN = re.compile(r"[\u3400-\u9fff]")
_SPACE_PATTERN = re.compile(r"\s+")
_SUFFIXES = (
    "哈萨克自治州",
    "蒙古族藏族自治州",
    "藏族羌族自治州",
    "苗族侗族自治州",
    "壮族苗族自治州",
    "布依族苗族自治州",
    "傣族景颇族自治州",
    "白族自治州",
    "藏族自治州",
    "彝族自治州",
    "壮族自治区",
    "回族自治区",
    "维吾尔自治区",
    "特别行政区",
    "自治区",
    "自治州",
    "地区",
    "林区",
    "新区",
    "省",
    "市",
    "区",
    "县",
    "盟",
    "旗",
)


@lru_cache(maxsize=1)
def _geo_tool():
    # 该依赖启动时会加载较大的地理数据，因此只在真正查询天气时导入。
    from GeoToolCN import GeoTool

    return GeoTool()


def _short_name(name: str) -> str:
    for suffix in _SUFFIXES:
        if name.endswith(suffix) and len(name) - len(suffix) >= 2:
            return name[: -len(suffix)]
    return name


def _region_mentions(address: str, level: str) -> list[Any]:
    mentions = []
    for region in _geo_tool().list_regions(level):
        aliases = {region.name, _short_name(region.name)}
        if any(len(alias) >= 2 and alias in address for alias in aliases):
            mentions.append(region)
    return mentions


def _parent_names(region: Any) -> tuple[str | None, str | None]:
    geo = _geo_tool()
    province = geo.get_region(f"{region.code[:2]}0000")
    city = None
    if region.level == "district":
        city = geo.get_region(f"{region.code[:4]}00")
    return (
        province.name if province is not None else None,
        city.name if city is not None else None,
    )


def _select_local_region(address: str):
    normalized = _SPACE_PATTERN.sub("", address)
    provinces = _region_mentions(normalized, "province")
    cities = _region_mentions(normalized, "city")
    districts = _region_mentions(normalized, "district")

    if provinces:
        province_prefixes = {region.code[:2] for region in provinces}
        cities = [r for r in cities if r.code[:2] in province_prefixes]
        districts = [r for r in districts if r.code[:2] in province_prefixes]
    if cities:
        city_prefixes = {region.code[:4] for region in cities}
        districts = [r for r in districts if r.code[:4] in city_prefixes]

    candidates = districts or cities or provinces
    if not candidates:
        candidates = _geo_tool().search(normalized, fuzzy=True)
    if not candidates:
        return None

    # 完整名称或简称完全相等时，优先于模糊包含结果。
    exact = [
        region
        for region in candidates
        if normalized in {region.name, _short_name(region.name)}
    ]
    if exact:
        candidates = exact

    unique = {region.code: region for region in candidates}
    if len(unique) > 1:
        labels = []
        for region in list(unique.values())[:5]:
            province, city = _parent_names(region)
            hierarchy = " / ".join(
                part for part in (province, city, region.name) if part
            )
            labels.append(hierarchy)
        raise ValueError(
            f"地点“{address}”存在多个匹配，请补充省或城市："
            + "；".join(labels)
        )
    return next(iter(unique.values()))


def _pinyin_query(location: str) -> str:
    cleaned = location.strip()
    for suffix in _SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    if not _CHINESE_PATTERN.search(cleaned):
        return cleaned
    return "".join(lazy_pinyin(cleaned, style=Style.NORMAL))


def resolve_china_location(location: str) -> dict[str, object]:
    """将中国行政区名称或地址解析为 WGS-84 经纬度。"""
    local_region = _select_local_region(location)
    if local_region is not None:
        province, city = _parent_names(local_region)
        return {
            "name": local_region.name,
            "admin1": province,
            "admin2": city,
            "country": "中国",
            "country_code": "CN",
            "latitude": local_region.latitude,
            "longitude": local_region.longitude,
            "adcode": local_region.code,
            "level": local_region.level,
            "resolved_by": "中国行政区划离线数据",
        }

    query = _pinyin_query(location)
    response = httpx.get(
        GEOCODING_URL,
        params={
            "name": query,
            "count": 10,
            "language": "zh",
            "format": "json",
            "countryCode": "CN",
        },
        timeout=8.0,
    )
    response.raise_for_status()
    matches = [
        item
        for item in response.json().get("results", [])
        if item.get("country_code") == "CN"
    ]
    if not matches:
        raise ValueError(f"找不到中国境内的地点：{location}")

    place = matches[0]
    return {
        **place,
        "resolved_by": "Open-Meteo 中国区拼音兜底",
    }

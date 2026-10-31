"""
Ядро геокодирования/маршрутизации для MAX-бота.
Независимо от мессенджера (только HTTP к Яндекс + ORS).
"""
from __future__ import annotations

import logging
import math
from typing import List, Optional, Tuple

import httpx

from config import settings

logger = logging.getLogger(__name__)


def _validate_coordinates(lat1, lon1, lat2, lon2) -> bool:
    try:
        vals = [lat1, lon1, lat2, lon2]
        for i, v in enumerate(vals):
            fv = float(v)
            if i % 2 == 0:
                if not (-90 <= fv <= 90):
                    return False
            else:
                if not (-180 <= fv <= 180):
                    return False
        return True
    except (TypeError, ValueError):
        return False


SUGGEST_URL = "https://suggest-maps.yandex.ru/v1/suggest"


def _extract_city(components) -> str:
    """Город из компонентов адреса: сначала locality, затем area/province."""
    names = {"locality": "", "area": "", "province": ""}
    for comp in components or []:
        kind = comp.get("kind")
        if kind in names and not names[kind]:
            names[kind] = comp.get("name", "") or ""
    return names["locality"] or names["area"] or names["province"]


def short_address(components) -> str:
    """Короткий адрес: «Город, улица, дом» без страны и региона.

    Приоритет города: locality (город/село) → district → area.
    Для населённых пунктов без улиц возвращает «Посёлок, дом».
    """
    locality = district = area = street = house = ""
    for comp in components or []:
        kind = comp.get("kind")
        name = (comp.get("name") or "").strip()
        if not name:
            continue
        if kind == "locality" and not locality:
            locality = name
        elif kind == "district" and not district:
            district = name
        elif kind == "area" and not area:
            area = name
        elif kind == "street" and not street:
            street = name
        elif kind in ("house", "building") and not house:
            house = name

    city = locality or district or area
    if house:
        return ", ".join(p for p in (city, street, house) if p)
    return ", ".join(p for p in (city, street) if p)


async def geocode_address(address: str) -> Tuple[Optional[dict], str]:
    """
    Геокодирует адрес. Возвращает (coords, city):
      coords = {"lat": float, "lon": float} или None
      city  = название населённого пункта (или "")
    """
    if not address or not address.strip():
        return None, ""
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            params = {
                "apikey": settings.yandex_api_key,
                "geocode": address,
                "format": "json",
                "results": 1,
            }
            resp = await client.get(settings.yandex_geocoder_url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error("Ошибка геокодирования '%s': %s", address, exc)
        return None, ""

    try:
        members = data["response"]["GeoObjectCollection"]["featureMember"]
        if not members:
            return None, ""
        geo = members[0]["GeoObject"]
        pos = geo["Point"]["pos"].split()
        lon, lat = float(pos[0]), float(pos[1])
        meta = geo.get("metaDataProperty", {}).get("GeocoderMetaData", {})
        addr = meta.get("Address", {})
        city = _extract_city(addr.get("Components", []))
        return {"lat": lat, "lon": lon}, city
    except (KeyError, IndexError, ValueError) as exc:
        logger.error("Не удалось разобрать ответ геокодера: %s", exc)
        return None, ""


async def geocode_address_with_context(address: str, context_city: str = "") -> Tuple[Optional[dict], str]:
    """Геокодирует адрес с учётом контекста города.

    Сначала пробует как есть, затем с добавлением контекста.
    """
    if not address or not address.strip():
        return None, ""
    coords, city = await geocode_address(address)
    if coords:
        return coords, city
    if context_city:
        coords2, city2 = await geocode_address(f"{context_city}, {address}")
        if coords2:
            return coords2, city2
    return None, ""


async def get_address_by_coordinates(latitude: float, longitude: float) -> Tuple[str, str]:
    """Обратное геокодирование: координаты -> (адрес, город)."""
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            params = {
                "apikey": settings.yandex_api_key,
                "geocode": f"{longitude},{latitude}",
                "format": "json",
                "results": 1,
                "kind": "house",
            }
            resp = await client.get(settings.yandex_geocoder_url, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error("Ошибка обратного геокодирования: %s", exc)
        return "", ""

    try:
        members = data["response"]["GeoObjectCollection"]["featureMember"]
        if not members:
            return "", ""
        geo = members[0]["GeoObject"]
        meta = geo.get("metaDataProperty", {}).get("GeocoderMetaData", {})
        components = meta.get("Address", {}).get("Components", [])
        addr_text = short_address(components) or meta.get("text", "")
        city = _extract_city(components)
        return addr_text, city
    except (KeyError, IndexError) as exc:
        logger.error("Не удалось разобрать ответ обратного геокодера: %s", exc)
        return "", ""


def calculate_approximate_distance_and_time(lat1, lon1, lat2, lon2) -> Tuple[float, int]:
    """Расчёт по формуле гаверсинуса (фолбэк)."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        raise ValueError("calculate_approximate_distance_and_time: координаты не могут быть None")
    R = 6371.0
    lat1r, lon1r = math.radians(lat1), math.radians(lon1)
    lat2r, lon2r = math.radians(lat2), math.radians(lon2)
    dlat, dlon = lat2r - lat1r, lon2r - lon1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    distance_km = R * 2 * math.asin(math.sqrt(a))
    duration_minutes = max(2, min(int((distance_km / 40) * 60), 120))
    return distance_km, duration_minutes


async def calculate_distance_and_time(lat1, lon1, lat2, lon2,
                                      waypoints: Optional[List[Tuple[float, float]]] = None
                                      ) -> Tuple[Optional[float], Optional[int]]:
    """Расстояние (км) и время (мин) по дорогам: ORS -> Яндекс -> гаверсинус.

    Если передан ``waypoints`` (список промежуточных точек), расчёт идёт
    по цепочке точек и дистанции суммируются.
    """
    if not _validate_coordinates(lat1, lon1, lat2, lon2):
        logger.error("Некорректные координаты маршрута")
        return None, None

    points: List[Tuple[float, float]] = [(lat1, lon1)]
    if waypoints:
        points.extend(waypoints)
    points.append((lat2, lon2))

    total_distance = 0.0
    total_duration = 0

    # Оркестрируем расчёт по каждому сегменту.
    async def _one_segment(a_lat: float, a_lon: float, b_lat: float, b_lon: float
                           ) -> Tuple[Optional[float], Optional[int]]:
        # 1) OpenRouteService (если задан ключ)
        if settings.openrouteservice_key:
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    headers = {"Authorization": settings.openrouteservice_key}
                    url = "https://api.openrouteservice.org/v2/directions/driving-car"
                    params = {"start": f"{a_lon},{a_lat}", "end": f"{b_lon},{b_lat}"}
                    resp = await client.get(url, params=params, headers=headers, timeout=10.0)
                    resp.raise_for_status()
                    data = resp.json()
                seg = data["features"][0]["properties"]["segments"][0]
                return seg["distance"] / 1000, int(seg["duration"] // 60)
            except Exception as exc:
                logger.warning("ORS недоступен для сегмента, фолбэк на Яндекс: %s", exc)

        # 2) Яндекс Маршрутизатор
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                params = {
                    "apikey": settings.yandex_api_key,
                    "waypoints": f"{a_lon},{a_lat}|{b_lon},{b_lat}",
                    "mode": "driving",
                }
                resp = await client.get(settings.yandex_router_url, params=params, timeout=10.0)
                resp.raise_for_status()
                data = resp.json()
            leg = data["route"]["legs"][0]
            return leg["distance"]["value"] / 1000, int(leg["duration"]["value"] // 60)
        except Exception as exc:
            logger.warning("Яндекс-роутер недоступен для сегмента, фолбэк на OSRM: %s", exc)

        # 3) OSRM (публичный демо-сервер, без ключа)
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                url = f"https://router.project-osrm.org/route/v1/driving/{a_lon},{a_lat};{b_lon},{b_lat}"
                resp = await client.get(url, params={"overview": "false"}, timeout=10.0)
                resp.raise_for_status()
                data = resp.json()
            route = data["routes"][0]
            return route["distance"] / 1000, max(2, int(route["duration"] // 60))
        except Exception as exc:
            logger.warning("OSRM недоступен для сегмента, фолбэк на гаверсинус: %s", exc)

        # 4) Гаверсинус
        d = calculate_approximate_distance_and_time(a_lat, a_lon, b_lat, b_lon)
        return d[0], d[1]

    for (a_lat, a_lon), (b_lat, b_lon) in zip(points[:-1], points[1:]):
        seg_dist, seg_dur = await _one_segment(a_lat, a_lon, b_lat, b_lon)
        if seg_dist is None:
            return None, None
        total_distance += seg_dist
        if seg_dur is not None:
            total_duration += seg_dur

    if total_distance == 0.0:
        # Фолбэк: гаверсинус на дистанцию точка-точка
        return calculate_approximate_distance_and_time(lat1, lon1, lat2, lon2)


async def suggest_address(query: str, city: str = "", limit: int = 8,
                          near_lat: Optional[float] = None,
                          near_lon: Optional[float] = None) -> list:
    """Подсказки адреса через Yandex Suggest.

    Ищет в пределах города (если известен) и отбрасывает чужие регионы.
    Возвращает: [{"title", "subtitle", "address", "kind"}]
    """
    text = (query or "").strip()
    if len(text) < 2:
        return []

    city_part = (city or "").strip()
    search_text = text
    if city_part and city_part.lower() not in text.lower():
        search_text = f"{text}, {city_part}"

    params = {
        "text": search_text,
        "apikey": settings.yandex_api_key,
        "lang": "ru_RU",
        "results": str(max(4, min(limit * 3, 20))),
        "print_address": "1",
        "types": "biz,geo",
    }
    if near_lat is not None and near_lon is not None:
        params["ll"] = f"{near_lon},{near_lat}"
        params["spn"] = "0.8,0.8"

    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            resp = await client.get(SUGGEST_URL, params=params, timeout=8.0)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("Suggest недоступен для %r: %s", search_text, exc)
        return []

    own_city = city_part.lower()
    results = []
    for item in data.get("results") or []:
        title = ((item.get("title") or {}).get("text") or "").strip()
        subtitle = ((item.get("subtitle") or {}).get("text") or "").strip()
        address = ((item.get("address") or {}).get("formatted_address") or "").strip()
        tags = item.get("tags") or []
        if not title:
            continue

        # Отбрасываем сам город и регион — они не адрес
        if tags and tags[0] in ("locality", "province", "country", "area"):
            continue

        haystack = f"{subtitle} {address}".lower()
        if own_city and haystack.strip() and own_city not in haystack:
            continue

        results.append({
            "title": title,
            "subtitle": subtitle,
            "address": address or f"{title}, {subtitle}".strip(", "),
            "kind": tags[0] if tags else "",
        })
        if len(results) >= limit:
            break

    # Если в своём городе ничего не нашли — показываем результаты без фильтра,
    # чтобы пассажир мог указать адрес в другом городе (межгород)
    if not results and city_part:
        for item in (data.get("results") or [])[:limit]:
            title = ((item.get("title") or {}).get("text") or "").strip()
            subtitle = ((item.get("subtitle") or {}).get("text") or "").strip()
            address = ((item.get("address") or {}).get("formatted_address") or "").strip()
            tags = item.get("tags") or []
            if not title:
                continue
            if tags and tags[0] in ("locality", "province", "country", "area"):
                continue
            results.append({
                "title": title,
                "subtitle": subtitle,
                "address": address or f"{title}, {subtitle}".strip(", "),
                "kind": tags[0] if tags else "",
            })

    return results

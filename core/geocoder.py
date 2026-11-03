"""
Ядро геокодирования/маршрутизации для MAX-бота.
Независимо от мессенджера (только HTTP к Яндекс + ORS).
"""
from __future__ import annotations

import logging
import math
import ssl
import time
from typing import List, Optional, Tuple

import certifi
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

# --- Кэши (важно для скорости при 50+ одновременных пассажирах) ---
# Центр города (lat, lon) — чтобы сортировать подсказки «от родного города».
_CITY_CENTER: dict = {}
# Кэш подсказок: (запрос, город) -> (время, список)
_SUGGEST_CACHE: dict = {}
_SUGGEST_TTL = 120.0        # секунд
# Кэш геокодирования адресов (одинаковые адреса у 50+ пассажиров)
_GEOCODE_CACHE: dict = {}
_GEOCODE_TTL = 600.0        # секунд
# Один общий HTTP-клиент: без пересоздания соединений каждый запрос.
# ВАЖНО: клиент привязан к event loop. В проекте запросы выполняются и в
# основном loop aiohttp, и в отдельном loop (maximus_server.run через
# asyncio.run) — поэтому держим клиент НА КАЖДЫЙ loop, иначе появляется
# ошибка «Event loop is closed» и подсказки/геокодинг молча не работают.
_http_clients: dict = {}


_HTTP_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _get_client() -> httpx.AsyncClient:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    key = id(loop) if loop is not None else 0
    entry = _http_clients.get(key)
    client = None
    if entry is not None:
        # Храним и сам loop: id() может совпасть у нового loop (старый удалён
        # сборщиком), и тогда мы взяли бы клиент от закрытого loop.
        if entry[0] is loop and not entry[1].is_closed:
            client = entry[1]
    if client is None:
        # connect-таймаут не должен быть слишком маленьким: TLS-рукопожатие
        # с suggest-maps.yandex.ru на Windows занимает 2-4 секунды.
        client = httpx.AsyncClient(
            trust_env=False,
            verify=_HTTP_SSL_CONTEXT,
            timeout=httpx.Timeout(8.0, connect=5.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        _http_clients[key] = (loop, client)
    # Не даём словарю расти: закрытые loop'ы накапливают мёртвые клиенты.
    if len(_http_clients) > 16:
        for k in list(_http_clients.keys()):
            if k != key:
                old_loop, old_client = _http_clients[k]
                if old_client.is_closed or old_loop is None or old_loop.is_closed():
                    _http_clients.pop(k, None)
    return client


async def _fetch_json(url: str, params: dict, attempts: int = 2):
    """GET JSON с одним повтором при сетевой ошибке.

    Публичные API Яндекса иногда отваливаются по таймауту (особенно первое
    TLS-соединение на Windows) — без повтора подсказки внезапно «пропадают».
    """
    import asyncio
    last_exc = None
    for attempt in range(attempts):
        try:
            client = _get_client()
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — сеть/HTTP/JSON
            last_exc = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.35)
    raise last_exc


async def city_center(city: str) -> Optional[dict]:
    """Координаты центра города (кэшируются в памяти)."""
    key = (city or "").strip().lower()
    if not key:
        return None
    if key in _CITY_CENTER:
        return _CITY_CENTER[key]
    coords, _ = await geocode_address(f"{city}, Россия")
    if coords:
        _CITY_CENTER[key] = coords
    return coords


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Быстрое расстояние по прямой (км) — для сортировки подсказок."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return _distance_km(lat1, lon1, lat2, lon2)


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
    key = " ".join(address.lower().split())
    cached = _GEOCODE_CACHE.get(key)
    if cached and (time.time() - cached[0]) < _GEOCODE_TTL:
        return cached[1], cached[2]
    try:
        params = {
            "apikey": settings.yandex_api_key,
            "geocode": address,
            "format": "json",
            "results": 1,
        }
        data = await _fetch_json(settings.yandex_geocoder_url, params)
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
        coords = {"lat": lat, "lon": lon}
        _GEOCODE_CACHE[key] = (time.time(), coords, city)
        if len(_GEOCODE_CACHE) > 5000:
            oldest = sorted(_GEOCODE_CACHE.items(), key=lambda kv: kv[1][0])[:1000]
            for k, _ in oldest:
                _GEOCODE_CACHE.pop(k, None)
        return coords, city
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
            resp = await client.get(settings.yandex_geocoder_url, params=params, timeout=2.5)
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

    # ВАЖНО: без этой строки функция молча возвращала None (маршрут посчитан,
    # но результат терялся) — избранное и заказы падали с TypeError.
    return total_distance, max(2, int(total_duration))


async def suggest_address(query: str, city: str = "", limit: int = 8,
                          near_lat: Optional[float] = None,
                          near_lon: Optional[float] = None) -> list:
    """Подсказки адреса через Yandex Suggest (быстро, с кэшем).

    Сортировка: ближайшие к «родному городу» (или к переданной точке) —
    сверху. Если пассажир из Черногорска вводит «Мира 1», первым будет
    «Мира 1, Черногорск», затем ближайшие населённые пункты по возрастанию
    расстояния.

    Возвращает: [{"title", "subtitle", "address", "kind", "distance_km"}]
    """
    text = (query or "").strip()
    if len(text) < 2:
        return []

    city_part = (city or "").strip()
    # Кэш: одинаковые запросы от 50 пассажиров не бьют в API по кругу.
    cache_key = (text.lower(), city_part.lower(), int(limit))
    cached = _SUGGEST_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _SUGGEST_TTL:
        return cached[1]

    # Центр «родного города»: по нему Яндекс сам считает расстояние до каждой
    # подсказки (поле distance.value, метры) — сортируем по возрастанию,
    # поэтому сверху оказываются ближайшие адреса.
    base = None
    if city_part:
        base = await city_center(city_part)

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
    if base:
        params["ll"] = f"{base['lon']},{base['lat']}"
        params["spn"] = "1.0,1.0"

    try:
        data = await _fetch_json(SUGGEST_URL, params)
    except Exception as exc:
        logger.warning("Suggest недоступен для %r: %s", search_text, exc)
        return []

    own_city = city_part.lower()
    collected = []
    for item in data.get("results") or []:
        title = ((item.get("title") or {}).get("text") or "").strip()
        subtitle = ((item.get("subtitle") or {}).get("text") or "").strip()
        addr_obj = item.get("address") or {}
        address = (addr_obj.get("formatted_address") or "").strip() if isinstance(addr_obj, dict) else ""
        tags = item.get("tags") or []
        if not title:
            continue
        # Сам город/регион — не адрес
        if tags and tags[0] in ("locality", "province", "country", "area"):
            continue

        # Яндекс отдаёт расстояние в метрах при переданном ll
        dist_km = None
        d = item.get("distance") or {}
        if isinstance(d, dict) and d.get("value") is not None:
            try:
                dist_km = round(float(d["value"]) / 1000.0, 1)
            except (TypeError, ValueError):
                dist_km = None

        haystack = f"{subtitle} {address}".lower()
        collected.append({
            "title": title,
            "subtitle": subtitle,
            "address": address or f"{title}, {subtitle}".strip(", "),
            "kind": tags[0] if tags else "",
            "distance_km": dist_km,
            "in_city": bool(own_city) and (own_city in haystack),
        })

    def sort_key(row):
        # 1) свой город — выше; 2) ближе к центру города — выше;
        # 3) без данных о расстоянии — в конце.
        return (
            0 if row.get("in_city") else 1,
            0 if row.get("distance_km") is not None else 1,
            row.get("distance_km") if row.get("distance_km") is not None else 10 ** 6,
        )

    collected.sort(key=sort_key)
    # Если в своём городе ничего не нашли — отдаём всё (межгород/другой город)
    results = [r for r in collected if r.get("in_city")] or collected
    results = results[:limit]
    for r in results:
        r.pop("in_city", None)
    _SUGGEST_CACHE[cache_key] = (time.time(), results)
    # Не даём кэшу расти бесконечно
    if len(_SUGGEST_CACHE) > 2000:
        oldest = sorted(_SUGGEST_CACHE.items(), key=lambda kv: kv[1][0])[:500]
        for k, _ in oldest:
            _SUGGEST_CACHE.pop(k, None)
    return results

"""Сервер мини-приложения. Логика и база — те же, что у бота MAX."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import traceback
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
BOT = ROOT
FRONTEND = os.path.join(ROOT, "frontend")
DATA = os.path.join(ROOT, "data", "taxi_data.json")
os.environ["STORAGE_FILE"] = DATA
if BOT not in sys.path:
    sys.path.insert(0, BOT)

from core.storage import storage as S  # noqa: E402
from core.storage import get_available_orders_for_driver  # noqa: E402
from core.tariff_manager import calculate_trip_price, resolve_destination_city, _is_intercity  # noqa: E402
from core.driver_support import (  # noqa: E402
    add_order_to_driver_queue,
    remove_order_from_driver_queue,
    stop_waiting,
    cancel_waiting_ticker,
    is_driver_busy,
    calculate_waiting_cost,
)
from core import geocoder  # noqa: E402
import httpx  # noqa: E402

_GEO_CACHE: dict = {}
_ROUTE_CACHE: dict = {}

ACTIVE = ("pending", "accepted", "driver_arrived", "in_progress")
PORT = int(os.environ.get("MINIAPP_PORT", "8765"))


def run(coro):
    """Безопасно выполняет корутину, независимо от того, запущен ли event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Нет запущенного event loop (например, бот с ThreadingHTTPServer)
        return asyncio.run(coro)
    # Есть запущенный event loop (например, aiohttp в api_server.py)
    # Запускаем корутину в отдельном потоке, чтобы не блокировать текущий loop
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def uid_from(handler, body: dict | None = None) -> int:
    """ID пользователя: cookie `mx` -> ?uid= в URL -> поле uid в теле запроса."""
    raw = handler.headers.get("Cookie") or ""
    jar = SimpleCookie()
    jar.load(raw)
    morsel = jar.get("mx")
    if morsel:
        try:
            uid = int(morsel.value)
            if uid > 0:
                return uid
        except ValueError:
            pass

    query = parse_qs(urlparse(handler.path).query)
    for key in ("uid", "user_id"):
        for value in query.get(key, []):
            try:
                uid = int(value)
                if uid > 0:
                    return uid
            except ValueError:
                continue

    if body:
        for key in ("uid", "user_id"):
            try:
                uid = int(body.get(key) or 0)
                if uid > 0:
                    return uid
            except (TypeError, ValueError):
                continue
    return 0


def brief_order(o: dict) -> dict:
    if not o:
        return {}
    passenger = S.get_user(o.get("passenger_id")) or {}
    driver = S.get_user(o.get("driver_id")) or {} if o.get("driver_id") else {}
    return {
        "id": o.get("id"),
        "passenger_id": o.get("passenger_id"),
        "driver_id": o.get("driver_id"),
        "pickup": o.get("pickup") or "",
        "destination": o.get("destination") or "",
        "pickup_coords": o.get("pickup_coords"),
        "destination_coords": o.get("destination_coords"),
        "price": o.get("final_price") or o.get("price") or 0,
        "distance": o.get("distance") or 0,
        "time_minutes": o.get("time_minutes") or 0,
        "status": o.get("status"),
        "created_at": o.get("created_at"),
        "pickup_city": o.get("pickup_city") or "",
        "destination_city": o.get("destination_city") or "",
        "waiting_cost": o.get("waiting_cost") or 0,
        "rating": o.get("passenger_rating"),
        "complaint": o.get("complaint") or "",
        "waypoints": o.get("waypoints") or [],
        "passenger_name": passenger.get("name") or "",
        "passenger_phone": passenger.get("phone") or "",
        "driver_name": driver.get("driver_name") or driver.get("name") or "",
        "driver_phone": driver.get("phone") or "",
        "car_model": driver.get("car_model") or "",
        "car_number": driver.get("car_number") or "",
        "car_color": driver.get("car_color") or "",
        "chat": o.get("chat") or [],
    }


def user_card(uid: int) -> dict:
    u = S.get_user(uid) or {}
    return {
        "id": uid,
        "name": u.get("name") or "",
        "phone": u.get("phone") or "",
        "rating": u.get("rating", 5),
        "roles": S.compute_roles(uid),
        "role": S.get_active_role(uid),
        "driver_status": u.get("driver_verification_status") or "",
        "reject_reason": u.get("driver_reject_reason") or "",
        "line": u.get("status") or "offline",
        "car_model": u.get("car_model") or "",
        "car_number": u.get("car_number") or "",
        "car_color": u.get("car_color") or "",
        "driver_name": S.get_driver_name(uid),
        "driver_phone": S.get_driver_phone(uid),
        "balance": S.get_balance(uid),
        "balance_hidden": S.balances_disabled() and not (
            S.is_admin(uid) or S.is_manager(uid)),
        "access": S.has_active_access(uid),
        "blocked": S.is_driver_blocked(uid),
        "geo": (u.get("last_location") or {}).get("address") or "",
        "lat": (u.get("last_location") or {}).get("lat"),
        "lon": (u.get("last_location") or {}).get("lon"),
        "docs": {
            "selfie": bool(u.get("doc_selfie")),
            "left": bool(u.get("doc_left")),
            "right": bool(u.get("doc_right")),
            "front": bool(u.get("doc_front")),
            "back": bool(u.get("doc_back")),
            "salon": bool(u.get("doc_salon") or u.get("car_photo_token")),
        },
    }


def passenger_block(uid: int) -> dict:
    orders = [brief_order(o) for o in S.get_orders_by_passenger(uid)]
    orders.sort(key=lambda o: o.get("id") or 0, reverse=True)
    active = S.get_passenger_active_order(uid)
    now = datetime.now()

    def in_period(o, days):
        raw = o.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return False
        return (now - dt).days < days and o.get("status") == "completed"

    stats = {}
    for key, days in (("today", 1), ("week", 7), ("month", 30)):
        rows = [o for o in orders if in_period(o, days)]
        stats[key] = {
            "count": len(rows),
            "sum": round(sum(float(o.get("price") or 0) for o in rows), 2),
            "km": round(sum(float(o.get("distance") or 0) for o in rows), 1),
        }
    return {
        "active": brief_order(active) if active else None,
        "orders": orders[:40],
        "favorites": S.get_favorites(uid),
        "stats": stats,
        "welcome": S.admin_data.get("passenger_welcome") or "",
        "support": S.admin_data.get("support_contact") or "",
    }


def driver_block(uid: int) -> dict:
    raw = get_available_orders_for_driver(uid) or []
    available = []
    for item in raw:
        oid = item.get("id") if isinstance(item, dict) else item
        order = item if isinstance(item, dict) and item.get("pickup") else S.get_order(oid)
        if order:
            available.append(brief_order(order))
    current = S.get_driver_active_order(uid)
    history = [brief_order(o) for o in S.get_driver_history(uid, 30)]
    tariffs = S.get_access_tariffs()
    return {
        "available": available,
        "current": brief_order(current) if current else None,
        "history": history,
        "stats": {p: S.get_driver_period_stats(uid, p) for p in ("day", "week", "month", "all")},
        "transactions": S.get_transaction_history(uid, 12),
        "tariffs": {str(k): v for k, v in tariffs.items()},
        "welcome": S.admin_data.get("driver_welcome") or "",
    }


def admin_block() -> dict:
    drivers = S.get_all_drivers()
    for d in drivers:
        d["user_id"] = int(d["user_id"])
    passengers = S.get_all_passengers()
    orders = [brief_order(o) for o in S.get_orders(limit=40)]
    cities = sorted({(o.get("pickup_city") or "без города") for o in S.orders.values()})
    return {
        "drivers": drivers,
        "pending": [d for d in drivers if d.get("verification_status") == "pending"],
        "passengers": passengers[:80],
        "orders": orders,
        "cities": cities,
        "commission": S.get_commission_config(),
        "balances_disabled": S.balances_disabled(),
        "tariffs": S.admin_data.get("tariffs_by_city") or {},
        "intercity": S.admin_data.get("tariff_intercity") or {},
        "welcome_passenger": S.admin_data.get("passenger_welcome") or "",
        "welcome_driver": S.admin_data.get("driver_welcome") or "",
        "support": S.admin_data.get("support_contact") or "",
        "managers": S.get_managers(),
        "admins": S.get_admin_ids(),
    }


def state_for(uid: int) -> dict:
    role = S.get_active_role(uid)
    data = {"ok": True, "user": user_card(uid)}
    if role == "driver":
        data["driver"] = driver_block(uid)
    elif role == "admin":
        data["admin"] = admin_block()
    else:
        data["passenger"] = passenger_block(uid)
    return data


def must_role(uid: int, role: str):
    if role not in S.compute_roles(uid):
        raise PermissionError("Эта роль вам не открыта")


async def geo_cached(address: str):
    key = " ".join((address or "").lower().split())
    if not key:
        return None, ""
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    coords, city = await geocoder.geocode_address(address)
    if coords:
        _GEO_CACHE[key] = (coords, city)
    return coords, city


async def route_fast(points: list) -> tuple:
    """Короткий маршрут: повтор из памяти, иначе OSRM не дольше 1.2 с, иначе прямая."""
    key = tuple((round(lat, 4), round(lon, 4)) for lat, lon in points)
    if key in _ROUTE_CACHE:
        return _ROUTE_CACHE[key]
    path = ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{path}"
        async with httpx.AsyncClient(trust_env=False) as client:
            resp = await client.get(url, params={"overview": "false"}, timeout=1.2)
            resp.raise_for_status()
            route = resp.json()["routes"][0]
            result = (float(route["distance"]) / 1000.0, max(2, int(route["duration"] // 60)))
    except Exception:
        dist = 0.0
        minutes = 0
        for a, b in zip(points, points[1:]):
            part, mins = geocoder.calculate_approximate_distance_and_time(a[0], a[1], b[0], b[1])
            dist += part
            minutes += mins
        result = (dist, max(2, minutes))
    _ROUTE_CACHE[key] = result
    return result


def quote(pickup: str, dest: str, stop: str = "",
          pickup_coords: dict | None = None,
          dest_coords: dict | None = None) -> dict:
    async def work():
        # Если координаты уже известны (геолокация), используем их напрямую
        pc = _coords_or_none(pickup_coords)
        dc = _coords_or_none(dest_coords)
        city1 = city2 = ""

        # Сначала определяем город подачи — от него зависит поиск назначения
        if pc is None:
            pc, city1 = await geo_cached(pickup)
        if not pc:
            raise ValueError("Не удалось найти адрес подачи")
        if not city1:
            city1 = await _city_by_coords(pc["lat"], pc["lon"])

        # Адрес назначения: если пассажир написал город в адресе — ищем как есть.
        # Если город не написан — ищем в городе подачи, чтобы «Юбилейная 29»
        # не нашлась в другом регионе за тысячи километров.
        if not dc:
            if city1 and not _city_mentioned(dest, city1) and not _has_explicit_other_city(dest, city1):
                dc, city2 = await geo_cached(f"{dest}, {city1}")
            if not dc:
                dc, city2 = await geo_cached(dest)
        if not dc:
            raise ValueError("Не удалось найти адрес назначения")
        if not city2:
            city2 = await _city_by_coords(dc["lat"], dc["lon"])

        # Не превращаем поездку в межгород, если пассажир не указал другой город
        dest_city = resolve_destination_city(city1, city2, dest)

        # Защита: если город совпадает с подачей, но точка уехала далеко —
        # значит геокодер нашёл одноимённую улицу в другом регионе.
        if dest_city == city1 and city1:
            far = _haversine(pc["lat"], pc["lon"], dc["lat"], dc["lon"])
            if far > 300:
                raise ValueError(
                    f"Адрес «{dest}» найден слишком далеко ({far:.0f} км). "
                    f"Укажите город и улицу точнее.")

        wps = []
        if stop:
            sc, _scity = await geo_cached(stop)
            if not sc:
                raise ValueError("Не удалось найти остановку")
            wps.append({"text": stop, "lat": sc["lat"], "lon": sc["lon"]})

        points = [(pc["lat"], pc["lon"])] + [(w["lat"], w["lon"]) for w in wps] + [(dc["lat"], dc["lon"])]
        dist, minutes = await route_fast(points)
        if dist is None:
            raise ValueError("Не удалось построить маршрут")

        price = calculate_trip_price(dist, minutes, pickup_city=city1,
                                     destination_city=dest_city)
        return {
            "pickup": pickup, "destination": dest, "stop": stop,
            "pickup_city": city1, "destination_city": dest_city,
            "pickup_coords": pc, "destination_coords": dc,
            "waypoints": wps, "km": round(float(dist), 1),
            "minutes": int(minutes or 0), "price": price,
            "is_intercity": _is_intercity(city1, dest_city),
        }
    return run(work())


def _coords_or_none(value) -> dict | None:
    """Приводит координаты к {'lat': float, 'lon': float} или None."""
    if not isinstance(value, dict):
        return None
    try:
        lat = float(value.get("lat", value.get("latitude")))
        lon = float(value.get("lon", value.get("longitude")))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:
        return None
    return {"lat": lat, "lon": lon}


async def _city_by_coords(lat: float, lon: float) -> str:
    """Город по координатам (обратный геокодинг)."""
    try:
        _addr, city = await geocoder.get_address_by_coordinates(lat, lon)
        return city or ""
    except Exception:
        return ""


def _city_mentioned(text: str, city: str) -> bool:
    """Указан ли город в тексте адреса."""
    if not text or not city:
        return False
    return city.strip().lower() in text.lower()


# Известные города зоны обслуживания — чтобы распознать явно указанный город
KNOWN_CITIES = (
    "абакан", "черногорск", "минусинск", "саяногорск", "аскиз",
    "усть-абакан", "бейский", "подсинцовка", "вершина теи", "копьёво",
    "копьево", "сорск", "шарыпово", "красноярск", "кызыл",
)


def _has_explicit_other_city(text: str, own_city: str) -> bool:
    """Есть ли в адресе город, отличный от города подачи.

    Нужно, чтобы «Абакан, Ленина 28» не искался как
    «Абакан, Ленина 28, Черногорск» — иначе геокодер вернёт Черногорск.
    """
    if not text:
        return False
    low = text.lower()
    own = (own_city or "").strip().lower()
    for city in KNOWN_CITIES:
        if city != own and city in low:
            return True
    return False


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между точками в километрах."""
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def create_order(uid: int, draft: dict, price=None) -> dict:
    if S.get_passenger_active_order(uid):
        raise ValueError("У вас уже есть активный заказ")
    user = S.get_user(uid) or {}
    oid = S.create_order({
        "passenger_id": uid,
        "pickup": draft["pickup"],
        "destination": draft["destination"],
        "pickup_coords": draft.get("pickup_coords"),
        "destination_coords": draft.get("destination_coords"),
        "distance": draft.get("km") or 0,
        "time_minutes": draft.get("minutes") or 0,
        "price": price if price is not None else draft.get("price") or 0,
        "pickup_city": draft.get("pickup_city"),
        "destination_city": draft.get("destination_city"),
        "waypoints": draft.get("waypoints") or [],
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "passenger_snapshot": {"name": user.get("name"), "phone": user.get("phone")},
    })
    return brief_order(S.get_order(oid))


def accept(uid: int, oid: int) -> dict:
    must_role(uid, "driver")
    if S.get_driver_verification_status(uid) != "approved":
        raise ValueError("Профиль водителя ещё не одобрен")
    if (S.get_user(uid) or {}).get("status") != "online":
        raise ValueError("Сначала выйдите на линию")
    if not S.has_active_access(uid):
        raise ValueError("Нет активной смены")
    if S.is_driver_blocked(uid) or is_driver_busy(uid):
        raise ValueError("Сейчас нельзя взять заказ")
    order = S.get_order(oid)
    if not order or order.get("status") != "pending":
        raise ValueError("Заказ уже занят или не найден")
    S.update_order(oid, driver_id=uid, status="accepted", accepted_at=datetime.now().isoformat())
    S.update_user(uid, driver_current_order_id=oid)
    add_order_to_driver_queue(uid, oid)
    return brief_order(S.get_order(oid))


def own_order(uid: int, oid: int, as_driver=False) -> dict:
    order = S.get_order(oid)
    if not order:
        raise ValueError("Заказ не найден")
    if as_driver and order.get("driver_id") != uid:
        raise ValueError("Это не ваш рейс")
    if not as_driver and order.get("passenger_id") != uid:
        raise ValueError("Это не ваш заказ")
    return order


def complete(uid: int, oid: int, lat: float, lon: float) -> dict:
    order = own_order(uid, oid, as_driver=True)
    if order.get("status") not in ("in_progress", "driver_arrived"):
        raise ValueError("Завершить можно поездку в пути")

    async def work():
        p = order.get("pickup_coords") or {}
        plat = p.get("lat", p.get("latitude"))
        plon = p.get("lon", p.get("longitude"))
        new_distance = float(order.get("distance") or 0)
        new_duration = int(order.get("time_minutes") or 0)
        if plat and plon:
            new_distance, new_duration = await route_fast([
                (float(plat), float(plon)), (lat, lon),
            ])
        address, city = await geocoder.get_address_by_coordinates(lat, lon)
        return address, city, new_distance, new_duration

    address, city, new_distance, new_duration = run(work())
    old_price = float(order.get("price") or 0)
    new_price = calculate_trip_price(
        new_distance, new_duration,
        pickup_city=order.get("pickup_city"),
        destination_city=city or order.get("destination_city"),
    )
    S.update_order(
        oid,
        destination=address or order.get("destination") or "",
        destination_city=city or order.get("destination_city") or "",
        destination_coords={"lat": lat, "lon": lon},
        distance=new_distance,
        time_minutes=new_duration,
        price=new_price,
        original_price=old_price,
        recalculated_at=datetime.now().isoformat(),
    )
    stop_waiting(oid)
    cancel_waiting_ticker(oid)
    waiting_cost = calculate_waiting_cost(oid) or 0
    total = new_price + waiting_cost
    commission = S.get_commission_config()
    if commission.get("enabled") and commission.get("mode") == "percent":
        earning = total * (100 - float(commission.get("percent") or 0)) / 100.0
    else:
        earning = total
    S.update_order(
        oid, status="completed", completed_at=datetime.now().isoformat(),
        waiting_cost=waiting_cost, final_price=total, driver_earning=earning, price=new_price,
    )
    S.add_balance(uid, earning)
    S.add_transaction(uid, "earning", earning, description=f"Поездка #{oid}")
    u = S.get_user(uid) or {}
    S.update_user(
        uid,
        orders_completed=(u.get("orders_completed") or 0) + 1,
        earnings=float(u.get("earnings") or 0) + earning,
        driver_current_order_id=None,
        last_lat=lat, last_lon=lon,
        last_location_at=datetime.now().isoformat(),
    )
    remove_order_from_driver_queue(uid, oid)
    return brief_order(S.get_order(oid))


def flag(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def act(uid: int, body: dict) -> dict:
    op = body.get("op") or ""
    if op == "login":
        user_id = int(body.get("user_id") or 0)
        if user_id not in S.users and user_id > 0:
            name = (body.get("name") or "").strip()
            S.ensure_user(user_id, name=name)
        if user_id not in S.users:
            raise ValueError("Пользователь не найден")
        return {"ok": True, "user_id": user_id}
    if op == "geo.resolve":
        # Геокодинг не требует авторизации
        if body.get("address"):
            text = str(body.get("address") or "").strip()
            coords, city = run(geocoder.geocode_address(text))
            if not coords:
                raise ValueError("Геокодирование не удалось")
            return {"ok": True, "lat": coords["lat"], "lon": coords["lon"], "address": text, "city": city or ""}
        lat, lon = float(body.get("lat")), float(body.get("lon"))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Неверные координаты")
        address, city = run(geocoder.get_address_by_coordinates(lat, lon))
        if not address:
            address = f"{lat:.5f}, {lon:.5f}"
        return {"ok": True, "lat": lat, "lon": lon, "address": address, "city": city or ""}
    if not uid:
        raise PermissionError("Сначала выберите профиль")
    if op == "role":
        role = body.get("role")
        roles = S.compute_roles(uid)
        if role not in roles:
            raise ValueError(f"Роль «{role}» недоступна. Доступны: {', '.join(roles)}")
        if not S.set_active_role(uid, role):
            raise ValueError("Не удалось переключить роль")
        return {"ok": True}
    if op == "profile":
        fields = {}
        if body.get("name"):
            fields["name"] = body["name"].strip()
        if body.get("phone"):
            fields["phone"] = body["phone"].strip()
        if fields:
            S.update_user(uid, **fields)
        return {"ok": True}
    if op == "quote":
        return {"ok": True, "draft": quote(
            body.get("pickup", ""), body.get("destination", ""),
            body.get("stop", ""),
            pickup_coords=body.get("pickup_coords"),
            dest_coords=body.get("dest_coords") or body.get("destination_coords"),
        )}
    if op == "order.create":
        draft = body.get("draft") or {}
        if not draft.get("pickup_coords"):
            draft = quote(draft.get("pickup", ""), draft.get("destination", ""), draft.get("stop", ""))
        price = body.get("price")
        return {"ok": True, "order": create_order(uid, draft, price)}
    if op == "order.price":
        order = own_order(uid, int(body["id"]))
        if order.get("status") not in ("pending", "accepted", "driver_arrived"):
            raise ValueError("Цену уже нельзя менять")
        price = int(body.get("price") or 0)
        if price < 50:
            raise ValueError("Сумма слишком маленькая")
        S.update_order(order["id"], price=price)
        return {"ok": True}
    if op == "order.cancel":
        order = own_order(uid, int(body["id"]))
        if order.get("status") not in ("pending", "accepted", "driver_arrived"):
            raise ValueError("Этот заказ уже нельзя отменить")
        S.update_order(order["id"], status="cancelled", cancelled_by="passenger", cancelled_at=datetime.now().isoformat())
        did = order.get("driver_id")
        if did:
            remove_order_from_driver_queue(did, order["id"])
            if (S.get_user(did) or {}).get("driver_current_order_id") == order["id"]:
                S.update_user(did, driver_current_order_id=None)
        stop_waiting(order["id"])
        return {"ok": True}
    if op == "order.rate":
        order = own_order(uid, int(body["id"]))
        if order.get("status") != "completed":
            raise ValueError("Оценить можно завершённую поездку")
        if order.get("passenger_rating"):
            raise ValueError("Оценка уже стоит")
        stars = int(body.get("stars") or 0)
        if stars < 1 or stars > 5:
            raise ValueError("Оценка от 1 до 5")
        S.set_order_rating(order["id"], stars, passenger_id=uid)
        return {"ok": True}
    if op == "order.complaint":
        order = own_order(uid, int(body["id"]))
        text = (body.get("text") or "").strip()
        if len(text) < 3:
            raise ValueError("Опишите, что не так")
        S.update_order(order["id"], complaint=text, complaint_at=datetime.now().isoformat())
        return {"ok": True}
    if op == "order.chat":
        order = S.get_order(int(body["id"]))
        if not order or uid not in (order.get("passenger_id"), order.get("driver_id")):
            raise ValueError("Нет доступа к переписке")
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError("Пустое сообщение")
        chat = list(order.get("chat") or [])
        chat.append({"from": uid, "text": text, "at": datetime.now().isoformat()})
        S.update_order(order["id"], chat=chat[-40:])
        return {"ok": True}
    if op == "favorite.add":
        order = own_order(uid, int(body["id"]))
        label = f"{order.get('pickup', '')} → {order.get('destination', '')}"
        S.add_favorite(uid, order["id"], label=label[:80])
        return {"ok": True}
    if op == "favorite.use":
        favs = S.get_favorites(uid)
        idx = int(body.get("index") or 0)
        if idx < 0 or idx >= len(favs):
            raise ValueError("Избранное не найдено")
        src = S.get_order(int(favs[idx].get("order_id") or 0))
        if not src:
            raise ValueError("Исходный заказ не найден")
        draft = {
            "pickup": src.get("pickup"), "destination": src.get("destination"),
            "pickup_coords": src.get("pickup_coords"), "destination_coords": src.get("destination_coords"),
            "pickup_city": src.get("pickup_city"), "destination_city": src.get("destination_city"),
            "waypoints": src.get("waypoints") or [],
            "km": src.get("distance") or 0, "minutes": src.get("time_minutes") or 0,
            "price": src.get("price") or 0,
        }
        return {"ok": True, "order": create_order(uid, draft)}
    if op == "driver.apply":
        number = (body.get("plate") or "").strip()
        color = (body.get("color") or "").strip()
        model = (body.get("car") or "").strip()
        if len(number.replace(" ", "")) < 5 or len(color) < 2 or len(model) < 2:
            raise ValueError("Нужны марка, номер и цвет")
        S.update_user(uid, car_model=model, driver_name=S.get_passenger_name(uid) or model)
        S.submit_driver_application(uid, car_number=number, car_color=color, car_model=model, car_photo_token="miniapp")
        return {"ok": True}
    if op == "driver.line":
        must_role(uid, "driver")
        status = body.get("status")
        if status not in ("online", "offline", "break"):
            raise ValueError("Неизвестный статус")
        if S.is_driver_blocked(uid):
            raise ValueError("Профиль временно заблокирован")
        if status == "online" and not S.has_active_access(uid):
            raise ValueError("Нет активной смены")
        S.set_online_status(uid, status)
        return {"ok": True}
    if op == "driver.geo":
        must_role(uid, "driver")
        lat, lon = float(body["lat"]), float(body["lon"])
        async def rev():
            return await geocoder.get_address_by_coordinates(lat, lon)
        address, city = run(rev())
        now = datetime.now().isoformat()
        S.update_user(uid, last_location={"lat": lat, "lon": lon, "address": address or "", "city": city or "", "updated_at": now},
                      current_city=city or "", last_lat=lat, last_lon=lon, last_location_at=now)
        S.update_driver_location(uid, lat, lon)
        return {"ok": True, "address": address}
    if op == "driver.profile":
        must_role(uid, "driver")
        fields = {}
        for src, dst in (("name", "driver_name"), ("phone", "driver_phone"), ("car", "car_model"), ("plate", "car_number"), ("color", "car_color")):
            if body.get(src):
                fields[dst] = str(body[src]).strip()
        if fields:
            S.update_user(uid, **fields)
        return {"ok": True}
    if op == "driver.doc":
        must_role(uid, "driver")
        key = {"selfie": "doc_selfie", "left": "doc_left", "right": "doc_right", "front": "doc_front", "back": "doc_back", "salon": "doc_salon"}.get(body.get("key"))
        if not key:
            raise ValueError("Неизвестный документ")
        S.update_user(uid, **{key: True})
        return {"ok": True}
    if op == "driver.topup":
        must_role(uid, "driver")
        amount = float(body.get("amount") or 0)
        if amount < 50:
            raise ValueError("Минимум 50 ₽")
        S.add_balance(uid, amount)
        S.add_transaction(uid, "topup", amount, description="Пополнение из мини-приложения")
        return {"ok": True}
    if op == "driver.buy":
        must_role(uid, "driver")
        hours = int(body.get("hours") or 0)
        tariffs = S.get_access_tariffs()
        if hours not in tariffs:
            raise ValueError("Такого тарифа нет")
        price = float(tariffs[hours])
        if not S.deduct_balance(uid, price):
            raise ValueError("Не хватает баланса")
        S.add_access_hours(uid, hours)
        S.add_transaction(uid, "access", -price, hours=hours, description=f"Смена {hours} ч")
        return {"ok": True}
    if op == "driver.accept":
        return {"ok": True, "order": accept(uid, int(body["id"]))}
    if op == "driver.arrived":
        order = own_order(uid, int(body["id"]), as_driver=True)
        if order.get("status") != "accepted":
            raise ValueError("Сначала заказ должен быть принят")
        S.update_order(order["id"], status="driver_arrived", driver_arrived_at=datetime.now().isoformat())
        return {"ok": True}
    if op == "driver.wait":
        order = own_order(uid, int(body["id"]), as_driver=True)
        if order.get("waiting_started_at") and not order.get("waiting_ended_at"):
            stop_waiting(order["id"])
        else:
            S.update_order(order["id"], waiting_started_at=datetime.now().isoformat(), waiting_ended_at=None)
        return {"ok": True}
    if op == "driver.start":
        order = own_order(uid, int(body["id"]), as_driver=True)
        if order.get("status") not in ("accepted", "driver_arrived"):
            raise ValueError("Поездку сейчас нельзя начать")
        stop_waiting(order["id"])
        S.update_order(order["id"], status="in_progress", started_at=datetime.now().isoformat())
        return {"ok": True}
    if op == "driver.complete":
        return {"ok": True, "order": complete(uid, int(body["id"]), float(body["lat"]), float(body["lon"]))}
    if op == "driver.cancel":
        order = own_order(uid, int(body["id"]), as_driver=True)
        if order.get("status") not in ACTIVE:
            raise ValueError("Рейс уже закрыт")
        stop_waiting(order["id"])
        cancel_waiting_ticker(order["id"])
        S.update_order(order["id"], status="cancelled", cancelled_by="driver", cancelled_at=datetime.now().isoformat())
        S.update_user(uid, driver_current_order_id=None)
        remove_order_from_driver_queue(uid, order["id"])
        S.record_driver_cancel(uid)
        return {"ok": True}
    if op.startswith("admin."):
        must_role(uid, "admin")
        return admin_act(body)
    raise ValueError("Неизвестное действие")


def admin_act(body: dict) -> dict:
    op = body["op"]
    if op == "admin.approve":
        S.approve_driver(int(body["id"]))
    elif op == "admin.reject":
        S.reject_driver(int(body["id"]), body.get("reason") or "отклонено")
    elif op == "admin.block":
        minutes = body.get("minutes")
        S.block_driver(int(body["id"]), None if minutes in (None, "perm", "") else int(minutes))
    elif op == "admin.unblock":
        S.unblock_driver(int(body["id"]))
    elif op == "admin.priority":
        S.set_driver_priority(int(body["id"]), int(body.get("seconds") or 0))
    elif op == "admin.balance":
        S.add_balance(int(body["id"]), float(body.get("amount") or 0))
    elif op == "admin.grant":
        S.add_access_hours(int(body["id"]), int(body.get("hours") or 0))
    elif op == "admin.unlim":
        S.grant_unlimited_access(int(body["id"]))
    elif op == "admin.resetacc":
        S.reset_access(int(body["id"]))
    elif op == "admin.delete":
        S.delete_user(int(body["id"]))
    elif op == "admin.order.cancel":
        S.cancel_order(int(body["id"]))
    elif op == "admin.order.price":
        S.update_order_price(int(body["id"]), float(body.get("price") or 0))
    elif op == "admin.passenger.block":
        S.block_passenger(int(body["id"]), int(body["minutes"]) if body.get("minutes") else None)
    elif op == "admin.passenger.unblock":
        S.unblock_passenger(int(body["id"]))
    elif op == "admin.commission":
        S.set_commission(
            enabled=flag(body.get("enabled")),
            mode=body.get("mode") or "percent",
            percent=float(body.get("percent") or 10),
        )
    elif op == "admin.balances":
        S.set_balances_disabled(flag(body.get("hidden")))
    elif op == "admin.tariff":
        city = (body.get("city") or "").strip()
        if not city:
            raise ValueError("Укажите город")
        tariffs = dict(S.admin_data.get("tariffs_by_city") or {})
        tariffs[city] = {"price_per_km": float(body.get("price_per_km") or 0)}
        S.admin_data["tariffs_by_city"] = tariffs
        S.save()
    elif op == "admin.content":
        for key, field in (("passenger_welcome", "passenger"), ("driver_welcome", "driver"), ("support_contact", "support")):
            if body.get(field) is not None:
                S.admin_data[key] = body[field]
        S.save()
    elif op == "reset_roles":
        S.remove_admin(uid)
        S.update_user(uid, type="user", active_role="passenger",
                      driver_verification_status=None)
        return {"ok": True}
    elif op == "admin.manager.add":
        S.add_manager(int(body["id"]))
    elif op == "admin.manager.remove":
        S.remove_manager(int(body["id"]))
    elif op == "admin.admin.add":
        _uid, ok = S.add_admin_by_phone(str(body.get("phone") or ""))
        if not ok:
            raise ValueError("Телефон не найден среди пользователей")
    elif op == "admin.admin.remove":
        S.remove_admin(int(body["id"]))
    else:
        raise ValueError("Неизвестное действие админа")
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def via_public_url(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host not in ("127.0.0.1", "localhost", "")

    def send_json(self, code: int, payload: dict, cookie_user: int | None = None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if "gzip" in (self.headers.get("Accept-Encoding") or "") and len(raw) > 400:
            raw = gzip.compress(raw, compresslevel=1)
            extra_encoding = "gzip"
        else:
            extra_encoding = ""
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if extra_encoding:
            self.send_header("Content-Encoding", extra_encoding)
        self.send_header("Content-Length", str(len(raw)))
        if cookie_user is not None:
            local = not self.via_public_url()
            if local:
                self.send_header("Set-Cookie", f"mx={cookie_user}; Path=/; SameSite=Lax")
            else:
                self.send_header("Set-Cookie", f"mx={cookie_user}; Path=/; SameSite=None; Secure")
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("Слишком большой запрос")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/people":
                if self.via_public_url():
                    self.send_json(404, {"ok": False, "error": "Список профилей доступен только на этом компьютере"})
                    return
                people = []
                for user_id, user in S.users.items():
                    if int(user_id) <= 0:
                        continue
                    people.append({
                        "id": int(user_id),
                        "name": user.get("name") or user.get("driver_name") or f"id {user_id}",
                        "roles": S.compute_roles(int(user_id)),
                    })
                people.sort(key=lambda p: p["name"])
                self.send_json(200, {"ok": True, "people": people})
                return
            if path == "/api/state":
                uid = uid_from(self)
                if not uid:
                    self.send_json(401, {"ok": False, "error": "Нет сессии"})
                    return
                if uid not in S.users:
                    S.ensure_user(uid)
                self.send_json(200, state_for(uid))
                return
            self.serve_file(path)
        except Exception as exc:
            traceback.print_exc()
            self.send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_body()
            if path != "/api/act":
                self.send_json(404, {"ok": False, "error": "Не найдено"})
                return
            uid = uid_from(self, body)
            if body.get("op") == "login" and not body.get("max_id") and self.via_public_url():
                self.send_json(403, {"ok": False, "error": "Вход по списку доступен только на этом компьютере"})
                return
            if body.get("op") == "login" and body.get("max_id"):
                body["user_id"] = int(body["max_id"])
                if body.get("name"):
                    S.ensure_user(int(body["user_id"]), name=body["name"])
            result = act(uid, body)
            who = int(result.get("user_id") or uid or 0)
            if result.get("ok") and who and body.get("op") != "quote":
                result["state"] = state_for(who)
            cookie = result.get("user_id") if body.get("op") == "login" else None
            self.send_json(200, result, cookie_user=cookie)
        except PermissionError as exc:
            self.send_json(403, {"ok": False, "error": str(exc)})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self.send_json(500, {"ok": False, "error": str(exc)})

    def serve_file(self, path: str):
        if path == "/":
            path = "/index.html"
        safe = os.path.normpath(os.path.join(FRONTEND, path.lstrip("/")))
        if not safe.startswith(FRONTEND) or not os.path.isfile(safe):
            self.send_error(404)
            return
        ext = os.path.splitext(safe)[1].lower()
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".jpg": "image/jpeg",
                 ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
        data = open(safe, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", types.get(ext, "application/octet-stream"))
        if ext in (".html", ".js", ".css"):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"Maximus Taxi: http://127.0.0.1:{PORT}/  база: {DATA}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

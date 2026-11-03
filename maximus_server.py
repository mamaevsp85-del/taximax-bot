"""Сервер мини-приложения. Логика и база — те же, что у бота MAX."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
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
# STORAGE_FILE из окружения имеет приоритет (тесты, деплой с volume).
# Раньше здесь стояло безусловное присваивание — из-за него игнорировались
# тестовые/деплойные пути и данные писались в боевую базу.
os.environ.setdefault("STORAGE_FILE", DATA)
if BOT not in sys.path:
    sys.path.insert(0, BOT)

from core.storage import storage as S, with_entrance  # noqa: E402
from core.storage import get_available_orders_for_driver  # noqa: E402
from core.service_area import normalize_city  # noqa: E402
from core.tariff_manager import (  # noqa: E402
    calculate_trip_price, resolve_destination_city, _is_intercity, count_waypoints,
)
from core.driver_support import (  # noqa: E402
    add_order_to_driver_queue,
    remove_order_from_driver_queue,
    stop_waiting,
    cancel_waiting_ticker,
    is_driver_busy,
    calculate_waiting_cost,
    get_waiting_info,
    get_waiting_settings,
    generate_navigation_url,
    generate_google_navigation_url,
)
from core import geocoder  # noqa: E402
from core.areas import group_orders_by_area  # noqa: E402
import httpx  # noqa: E402

logger = logging.getLogger(__name__)

_GEO_CACHE: dict = {}
_ROUTE_CACHE: dict = {}

ACTIVE = ("pending", "accepted", "driver_arrived", "in_progress")
PORT = int(os.environ.get("MINIAPP_PORT", "8765"))


_loop_lock = None
_loop = None
_loop_thread = None


def _get_loop():
    """Один постоянный event loop в отдельном потоке.

    ВАЖНО: раньше на каждый вызов run() создавался НОВЫЙ loop через
    asyncio.run(). Из-за этого HTTP-клиент геокодера/подсказок создавался
    заново и каждый раз заново проходил TLS-рукопожатие — отсюда «думает
    по полминуты». Теперь loop один, соединения переиспользуются.
    """
    global _loop, _loop_thread, _loop_lock
    import threading
    if _loop_lock is None:
        _loop_lock = threading.Lock()
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True,
                                            name="maximus-asyncio")
            _loop_thread.start()
    return _loop


def run(coro):
    """Выполнить корутину в ЕДИНОМ постоянном loop'е.

    Раньше при вызове из api_server (aiohttp) создавался новый loop на каждый
    запрос — соединения не переиспользовались, и каждый раз заново шло
    TLS-рукопожатие (подсказки адреса «думали» по 10-30 секунд).
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    loop = _get_loop()
    if running is loop:
        # Уже внутри общего loop'а — ждать себя нельзя, выполняем напрямую
        return asyncio.run(coro)
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def uid_from(handler, body: dict | None = None) -> int:
    """ID пользователя: cookie mx -> cookie maximus_uid -> токен -> ?uid= -> тело.

    Токен персональный: он выдаётся пользователю ботом и однозначно
    определяет его. Никаких глобальных «последний пользователь».
    """
    raw = handler.headers.get("Cookie") or ""
    jar = SimpleCookie()
    jar.load(raw)

    # 1) Персональный токен (cookie / заголовок / query) — приоритет выше cookie,
    #    потому что cookie mx могла остаться от прошлой сессии или от другого
    #    сервера, а токен однозначно указывает, кто открыл приложение.
    token = ""
    morsel = jar.get("maximus_token")
    if morsel:
        token = morsel.value
    if not token:
        token = handler.headers.get("X-Maximus-Token") or ""
    query = parse_qs(urlparse(handler.path).query)
    if not token:
        for value in query.get("token", []):
            token = value
            break
    if token:
        try:
            from core.storage import storage as _storage
            uid = _storage.resolve_token(token)
            if uid:
                return int(uid)
        except Exception:
            pass

    # 2) Cookie с ID
    for cookie_name in ("mx", "maximus_uid"):
        morsel = jar.get(cookie_name)
        if morsel:
            try:
                uid = int(morsel.value)
                if uid > 0:
                    return uid
            except ValueError:
                pass

    # 3) uid в query / теле — только для локальной отладки
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


def _order_total(o: dict) -> float:
    """Итог, который пассажир платит за заказ.

    final_price в complete() уже включает платное ожидание, поэтому
    waiting_cost повторно не прибавляем. Для старых заказов без
    final_price собираем price + waiting.
    """
    try:
        final = float(o.get("final_price") or 0)
    except (TypeError, ValueError):
        final = 0.0
    if final:
        return final
    base = 0.0
    try:
        base = float(o.get("price") or 0)
    except (TypeError, ValueError):
        base = 0.0
    try:
        wait = float(o.get("waiting_cost") or 0)
    except (TypeError, ValueError):
        wait = 0.0
    return base + wait


def brief_order(o: dict) -> dict:
    if not o:
        return {}
    passenger = S.get_user(o.get("passenger_id")) or {}
    driver = S.get_user(o.get("driver_id")) or {} if o.get("driver_id") else {}
    try:
        final_price = float(o.get("final_price") or 0)
    except (TypeError, ValueError):
        final_price = 0.0
    try:
        waiting_cost = float(o.get("waiting_cost") or 0)
    except (TypeError, ValueError):
        waiting_cost = 0.0
    # Итог с пассажира. final_price уже включает ожидание.
    total_paid = final_price or (float(o.get("price") or 0) + waiting_cost)
    commission = S.get_commission_config()
    commission_on = bool(commission.get("enabled")) and commission.get("mode") == "percent"
    percent = float(commission.get("percent") or 0) if commission_on else 0.0
    earning = o.get("driver_earning")
    if earning is None or earning == "":
        earning = total_paid * (100 - percent) / 100.0 if commission_on else total_paid
    try:
        earning = float(earning)
    except (TypeError, ValueError):
        earning = total_paid
    return {
        "id": o.get("id"),
        "passenger_id": o.get("passenger_id"),
        "driver_id": o.get("driver_id"),
        "pickup": o.get("pickup") or "",
        "destination": o.get("destination") or "",
        "pickup_coords": o.get("pickup_coords"),
        "destination_coords": o.get("destination_coords"),
        "price": o.get("final_price") or o.get("price") or 0,
        "base_price": o.get("base_price") or o.get("price") or 0,
        "final_price": o.get("final_price") or o.get("price") or 0,
        # Итог с пассажира: final_price УЖЕ включает ожидание (см. complete()),
        # поэтому повторно waiting_cost не прибавляем.
        "total_price": round(
            float(o.get("final_price") or 0)
            or (float(o.get("price") or 0) + waiting_cost), 2),
        "commission_percent": percent,
        "commission_amount": round(
            (float(o.get("final_price") or 0)
             or (float(o.get("price") or 0) + waiting_cost)) - earning, 2),
        "driver_earning": round(earning, 2),
        "distance": o.get("distance") or 0,
        "time_minutes": o.get("time_minutes") or 0,
        "status": o.get("status"),
        "created_at": o.get("created_at"),
        "completed_at": o.get("completed_at"),
        "pickup_city": o.get("pickup_city") or "",
        "destination_city": o.get("destination_city") or "",
        "waiting_cost": o.get("waiting_cost") or 0,
        "rating": o.get("passenger_rating"),
        "complaint": o.get("complaint") or "",
        "waypoints": o.get("waypoints") or [],
        # Водителю телефон пассажира не показываем: связь — через «Написать»
        # и «Позвонить через MAX». Открыт только во время активного рейса.
        "passenger_name": passenger.get("name") or passenger.get("first_name") or "",
        "passenger_phone": (passenger.get("phone") or "")
        if o.get("status") in ("accepted", "driver_arrived", "in_progress") else "",
        "can_write": bool(o.get("passenger_id")),
        "driver_name": driver.get("driver_name") or driver.get("name") or "",
        "driver_phone": driver.get("phone") or "",
        "car_model": driver.get("car_model") or "",
        "car_number": driver.get("car_number") or "",
        "car_color": driver.get("car_color") or "",
        "chat": o.get("chat") or [],
        "same_account": o.get("passenger_id") == o.get("driver_id"),
        "passenger_waiting": bool(o.get("passenger_waiting")),
        "no_driver_asked_at": o.get("no_driver_asked_at"),
        "driver_arrived_at": o.get("driver_arrived_at"),
        # --- таймер ожидания (виден и водителю, и пассажиру) ---
        "waiting": _waiting_payload(o),
    }


def _waiting_payload(o: dict) -> dict:
    """Данные таймера ожидания для карточек водителя и пассажира.

    Считаем на месте (а не из кэша), чтобы Mini-App мог показывать
    «живой» отсчёт между опросами состояния.
    """
    if not o:
        return {}
    started = o.get("waiting_started_at")
    ended = o.get("waiting_ended_at")
    total = int(o.get("waiting_seconds_total", 0) or 0)
    running = bool(started and not ended)
    if running:
        try:
            dt = datetime.fromisoformat(started)
            total += max(0, int((datetime.now(dt.tzinfo) - dt).total_seconds()))
        except Exception:
            pass
    try:
        info = get_waiting_info(o.get("id"))
    except Exception:
        info = {}
    free_minutes = info.get("free_minutes", get_waiting_settings(
        o.get("pickup_city") or o.get("city") or "")["free_minutes"])
    per_minute = info.get("per_minute", get_waiting_settings(
        o.get("pickup_city") or o.get("city") or "")["per_minute"])
    free_seconds = float(free_minutes) * 60.0
    cost = float(info.get("cost") or 0)
    if not info and total > free_seconds:
        cost = round(((total - free_seconds) / 60.0) * float(per_minute), 2)
    return {
        "running": running,
        "seconds": total,
        "time_text": f"{total // 60}:{total % 60:02d}",
        "free_minutes": float(free_minutes),
        "per_minute": float(per_minute),
        "free_left_seconds": int(max(0, free_seconds - total)),
        "is_free": total <= free_seconds,
        "cost": round(cost, 2),
        "started_at": started,
        "ended_at": ended,
    }


def _normalize_phone(phone: str) -> str:
    """Приводит номер к виду 7XXXXXXXXXX."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits or str(phone or "").strip()


def _profile_complete(user: dict) -> bool:
    """Профиль заполнен: имя и телефон. Город — желателен, но не обязателен."""
    name = (user.get("name") or "").strip()
    phone = "".join(ch for ch in str(user.get("phone") or "") if ch.isdigit())
    return bool(name and len(phone) >= 10)


def _needs_onboarding(user: dict) -> bool:
    """Регистрация (имя + телефон + город) — ТОЛЬКО при первом входе.

    После прохождения ставится флаг ``onboarded``, и больше знакомство
    не показывается никогда — даже если пользователь потом очистил
    какое-то поле профиля (например, город).
    """
    if user.get("onboarded"):
        return False
    if user.get("onboarding_started_at") or user.get("onboarded_at"):
        # Начал, но не закончил — всё равно спрашиваем, пока не заполнит.
        return not _profile_complete(user)
    return not _profile_complete(user)


def user_card(uid: int) -> dict:
    u = S.get_user(uid) or {}
    return {
        "id": uid,
        "name": u.get("name") or "",
        "phone": u.get("phone") or "",
        "home_city": u.get("home_city") or "",
        "profile_complete": _profile_complete(u),
        "onboarded": bool(u.get("onboarded")),
        "needs_onboarding": _needs_onboarding(u),
        "bot_link": _bot_link(),
        "rating": u.get("rating", 5),
        "roles": S.compute_roles(uid),
        "role": S.get_active_role(uid),
        "driver_status": u.get("driver_verification_status") or "",
        "reject_reason": u.get("driver_reject_reason") or "",
        "line": u.get("status") or "offline",
        "car_model": u.get("car_model") or "",
        "car_number": u.get("car_number") or "",
        "car_color": u.get("car_color") or "",
        "car_photo": bool(u.get("car_photo_path") or (
            u.get("car_photo_token") and not str(u.get("car_photo_token")).startswith("miniapp"))),
        "car_photo_url": f"/api/photo/{uid}" if u.get("car_photo_path") else "",
        "driver_name": S.get_driver_name(uid),
        "driver_phone": S.get_driver_phone(uid),
        "balance": S.get_balance(uid),
        # Показывать баланс в интерфейсе водителя имеет смысл только когда
        # включена комиссия/платные смены. Без комиссии (бесплатный период)
        # баланс водителю не нужен и не показывается.
        "commission_enabled": bool(S.get_commission_config().get("enabled")),
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
    ref = S.get_driver_reference_point(uid) or {}
    ref_lat, ref_lon = ref.get("lat"), ref.get("lon")
    raw = get_available_orders_for_driver(uid, ref_lat, ref_lon) or []
    available = []
    for item in raw:
        oid = item.get("id") if isinstance(item, dict) else item
        order = item if isinstance(item, dict) and item.get("pickup") else S.get_order(oid)
        if order:
            available.append(brief_order(order))
    active = S.get_driver_active_orders(uid)
    current = active[0] if active else {}
    following = active[1] if len(active) > 1 else {}
    history = [brief_order(o) for o in S.get_driver_history(uid, 30)]
    today_orders = [brief_order(o) for o in S.get_driver_history(uid, 40, period="today")]
    tariffs = S.get_access_tariffs()
    today_stats = S.get_driver_stats(uid, "today")
    # Сколько заказов прямо сейчас ждут водителя (по всей базе) —
    # показываем в пункте «Список заказов».
    pending_waiting = 0
    try:
        for o in S.orders.values():
            if o.get("status") == "pending" and not o.get("driver_id"):
                pending_waiting += 1
    except Exception:
        pending_waiting = 0
    # Разбивка по дням для графика (день/неделя/месяц).
    daily = {}
    for key, days in (("day", 1), ("week", 7), ("month", 30)):
        try:
            daily[key] = S.get_driver_daily_stats(uid, days)
        except Exception:
            daily[key] = []
    return {
        "available": available,
        "available_count": len(available),
        "pending_waiting": pending_waiting,
        "current": brief_order(current) if current else None,
        "next": brief_order(following) if following else None,
        "reference": ref,
        "groups": group_orders_by_area(available, ref_lat, ref_lon),
        "history": history,
        "today_orders": today_orders,
        "today": today_stats,
        "daily": daily,
        "stats": {p: S.get_driver_period_stats(uid, p) for p in ("day", "week", "month", "all")},
        "transactions": S.get_transaction_history(uid, 12),
        "tariffs": {str(k): v for k, v in tariffs.items()},
        "welcome": S.admin_data.get("driver_welcome") or "",
    }


def admin_block() -> dict:
    from core.tariff_manager import _default_intercity_tariff, _default_tariff
    drivers = S.get_all_drivers()
    for d in drivers:
        d["user_id"] = int(d["user_id"])
    passengers = S.get_all_passengers()
    orders = [brief_order(o) for o in S.get_orders(limit=40)]
    cities = sorted({(o.get("pickup_city") or "без города") for o in S.orders.values()})
    # Отдаём тарифы С дефолтами: иначе в админке пусто и не видно
    # текущих наценок (день/ночь, точка по пути), пока их не сохранили.
    intercity = dict(_default_intercity_tariff())
    intercity.update({k: v for k, v in (S.admin_data.get("tariff_intercity") or {}).items()
                      if v is not None})
    city_tariffs = {}
    for key, value in (S.admin_data.get("tariffs_by_city") or {}).items():
        merged = dict(_default_tariff())
        merged.update({k: v for k, v in (value or {}).items() if v is not None})
        city_tariffs[key] = merged
    return {
        "drivers": drivers,
        "pending": [d for d in drivers if d.get("verification_status") == "pending"],
        "passengers": passengers[:80],
        "orders": orders,
        "cities": cities,
        "commission": S.get_commission_config(),
        "balances_disabled": S.balances_disabled(),
        "tariffs": city_tariffs,
        "intercity": intercity,
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
          dest_coords: dict | None = None,
          pickup_city: str = "",
          destination_city: str = "") -> dict:
    """Расчёт маршрута и цены.

    ``pickup_city``/``destination_city`` — подсказка города (например, из
    избранного маршрута или из заказа). ВАЖНО: без неё геокодер может найти
    «Мира 1а» в другом регионе (Владимир вместо Черногорска), и тогда
    применится чужой тариф — цена «не пересчитывается».
    """
    hint_pickup_city = (pickup_city or "").strip()
    hint_dest_city = (destination_city or "").strip()

    async def work():
        # Если координаты уже известны (геолокация), используем их напрямую
        pc = _coords_or_none(pickup_coords)
        dc = _coords_or_none(dest_coords)
        city1 = hint_pickup_city or ""
        city2 = hint_dest_city or ""

        # Подача и назначение ищутся одновременно: так расчёт не ждёт
        # два запроса к геокодеру друг за другом.
        if pc is None and dc is None:
            (pc, gcity1), (dc, gcity2) = await asyncio.gather(
                geo_cached(pickup), geo_cached(dest))
            city1 = city1 or gcity1
            city2 = city2 or gcity2
        elif pc is None:
            pc, gcity1 = await geo_cached(pickup)
            city1 = city1 or gcity1
        elif dc is None:
            dc, gcity2 = await geo_cached(dest)
            city2 = city2 or gcity2
        if not pc:
            raise ValueError("Не удалось найти адрес подачи")
        if not dc:
            dc, gcity2 = await geo_cached(dest)
            city2 = city2 or gcity2
        if pc and not city1 and dc and not city2:
            city1, city2 = await asyncio.gather(
                _city_by_coords(pc["lat"], pc["lon"]),
                _city_by_coords(dc["lat"], dc["lon"]),
            )
        elif pc and not city1:
            city1 = await _city_by_coords(pc["lat"], pc["lon"])
        elif dc and not city2:
            city2 = await _city_by_coords(dc["lat"], dc["lon"])

        # Адрес назначения ищем как написал пассажир. Если нашлась улица
        # в другом регионе и город в тексте не назван — повторяем поиск
        # в городе подачи. Соседние города (Абакан и Черногорск) остаются
        # разными: для них действует межгород.
        if dc and city1:
            far = _haversine(pc["lat"], pc["lon"], dc["lat"], dc["lon"])
            named = _has_explicit_other_city(dest, city1) or _city_mentioned(dest, city2)
            other = bool(city2) and normalize_city(city2) != normalize_city(city1)
            if other and far > 300 and not named:
                alt, alt_city = await geo_cached(f"{dest}, {city1}")
                if alt:
                    dc, city2 = alt, (alt_city or city1)
        if not dc:
            raise ValueError("Не удалось найти адрес назначения")
        if not city2:
            city2 = await _city_by_coords(dc["lat"], dc["lon"])

        far = _haversine(pc["lat"], pc["lon"], dc["lat"], dc["lon"])
        dest_city = resolve_destination_city(city1, city2, dest, distance_km=far)

        # Защита: если город совпадает с подачей, но точка уехала далеко —
        # значит геокодер нашёл одноимённую улицу в другом регионе.
        if city1 and normalize_city(dest_city or "") == normalize_city(city1):
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

        price = calculate_trip_price(
            dist, minutes, pickup_city=city1, destination_city=dest_city,
            waypoints_count=count_waypoints(wps),
        )
        return {
            "pickup": pickup, "destination": dest, "stop": stop,
            "pickup_city": city1, "destination_city": dest_city,
            "pickup_coords": pc, "destination_coords": dc,
            "waypoints": wps, "km": round(float(dist), 1),
            "minutes": int(minutes or 0), "price": price,
            "is_intercity": _is_intercity(city1, dest_city),
        }
    return run(work())


def _bot_link() -> str:
    """Ссылка на чат с ботом в MAX (для возврата из мини-приложения)."""
    link = (S.admin_data.get("bot_link") or "").strip()
    if link:
        return link
    username = (S.admin_data.get("bot_username") or "").strip().lstrip("@")
    if username:
        return f"https://max.ru/{username}"
    return ""


def _send_call_card(to_uid: int, peer_uid: int, name: str, phone: str, who: str) -> None:
    """Кладёт в чат с ботом карточку собеседника: звонок идёт из MAX, без max.ru/id."""
    import threading

    def worker():
        try:
            asyncio.run(_contact_async(to_uid, peer_uid, name, phone, who))
        except Exception as exc:
            logger.warning("Не удалось отправить карточку звонка %s: %s", to_uid, exc)

    threading.Thread(target=worker, daemon=True).start()


async def _contact_async(to_uid: int, peer_uid: int, name: str, phone: str, who: str) -> None:
    from max_api.client import MaxClient, MaxApiError
    from max_api.keyboards import contact_attachment

    card = contact_attachment(name, peer_uid, phone)
    vcf_only = {
        "type": "contact",
        "payload": {"name": card["payload"]["name"], "vcf_info": card["payload"].get("vcf_info") or ""},
    }
    id_only = {
        "type": "contact",
        "payload": {"name": card["payload"]["name"], "contact_id": int(peer_uid)},
    }
    variants = [card, id_only]
    if vcf_only["payload"].get("vcf_info"):
        variants.append(vcf_only)
    client = MaxClient()
    try:
        last = None
        for attachment in variants:
            try:
                await client.send_message(
                    user_id=to_uid,
                    text=f"📞 Контакт {who}. Нажмите на карточку и позвоните в MAX.",
                    text_format="html",
                    attachments=[attachment],
                )
                return
            except MaxApiError as exc:
                last = exc
                logger.warning("Карточка звонка не принята (%s), пробую другой формат", exc)
        if last:
            raise last
    finally:
        await client.close()


def _notify_passenger(order: dict, text: str, keyboard: list | None = None) -> None:
    """Отправляет уведомление пассажиру в чат MAX (в фоне)."""
    import threading

    pid = order.get("passenger_id")
    if not pid:
        return
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return
    if pid_int <= 0:
        return

    def worker():
        try:
            asyncio.run(_notify_async(pid_int, text, keyboard))
        except Exception as exc:
            logger.warning("Не удалось уведомить пассажира %s: %s", pid_int, exc)

    threading.Thread(target=worker, daemon=True).start()


async def _notify_async(user_id: int, text: str, keyboard: list | None = None) -> None:
    from max_api.client import MaxClient
    from max_api.keyboards import keyboard_attachment

    client = MaxClient()
    try:
        attachments = keyboard_attachment(keyboard) if keyboard else None
        await client.send_message(user_id=user_id, text=text, attachments=attachments)
    finally:
        await client.close()


def _send_geo_prompt(user_id: int, kind: str = "pickup") -> None:
    """Бот присылает в чат кнопку «Отправить геолокацию» (нативная MAX)."""
    import threading

    def worker():
        try:
            asyncio.run(_geo_prompt_async(user_id, kind))
        except Exception as exc:
            logger.warning("Не удалось отправить гео-кнопку %s: %s", user_id, exc)

    threading.Thread(target=worker, daemon=True).start()


async def _geo_prompt_async(user_id: int, kind: str) -> None:
    from max_api.client import MaxClient
    from max_api.keyboards import keyboard_attachment, request_geo_location_button

    label = "точку подачи" if kind == "pickup" else "точку назначения"
    client = MaxClient()
    try:
        await client.send_message(
            user_id=user_id,
            text=(f"📍 <b>Подтвердите {label}</b>\n\n"
                  "Нажмите кнопку ниже. MAX откроет окно подтверждения геолокации.\n"
                  "После подтверждения вернитесь в приложение — адрес уже будет в строке."),
            attachments=keyboard_attachment(
                [[request_geo_location_button("Подтвердить геолокацию", quick=False)]]),
        )
    finally:
        await client.close()


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


def _favorite_draft(uid: int, index: int) -> dict:
    """Маршрут из избранного для экрана подтверждения, без создания заказа.

    Цену ВСЕГДА пересчитываем по актуальному тарифу (тарифы могли измениться).
    Город подачи берём из избранного, а если его нет — из профиля пассажира
    (home_city). Без города геокодер находил «Мира 1а» в другом регионе
    (например, во Владимире) и применялся чужой тариф — цена «не менялась».
    """
    route = S.favorite_route(uid, index)
    user = S.get_user(uid) or {}
    home_city = (user.get("home_city") or "").strip()
    # Город-подсказка: сохранённый в избранном → город профиля.
    hint_city = (route.get("pickup_city") or home_city or "").strip()
    # Если сохранённый город не совпадает с домашним — доверяем домашнему:
    # это типичный случай, когда адрес когда-то нашёлся в чужом регионе.
    stale_coords = False
    if home_city and route.get("pickup_city") and \
            normalize_city(route["pickup_city"]) != normalize_city(home_city):
        hint_city = home_city
        # Координаты от «чужого» города больше не действительны —
        # без сброса геокодер снова вернёт Владимир.
        stale_coords = True
    wps = route.get("waypoints") or []
    draft = {
        "pickup": route["pickup"],
        "destination": route["destination"],
        "entrance": route["entrance"],
        "label": route["label"],
        "pickup_coords": route["pickup_coords"],
        "destination_coords": route["destination_coords"],
        "pickup_city": route["pickup_city"],
        "destination_city": route["destination_city"],
        "waypoints": wps,
        "km": route["km"],
        "minutes": route["minutes"],
        "price": route["price"],
        "step": 3,
    }
    # Полный пересчёт через геокодер/маршрут — актуальные км и тариф.
    stop_text = ""
    if wps:
        first = wps[0]
        if isinstance(first, dict):
            stop_text = str(first.get("text") or first.get("address") or "")
        else:
            stop_text = str(first or "")
    try:
        fresh = quote(
            route["pickup"], route["destination"], stop=stop_text,
            pickup_coords=None if stale_coords else route.get("pickup_coords"),
            dest_coords=None if stale_coords else route.get("destination_coords"),
            # Подсказка города: без неё геокодер может найти «Мира 1а»
            # во Владимире и применить чужой тариф — цена не пересчитается.
            pickup_city=hint_city,
            destination_city=hint_city or (route.get("destination_city") or ""),
        )
        # Если в избранном больше одной остановки — сохраняем их и
        # пересчитываем цену с полной наценкой за каждую точку.
        if len(wps) > 1:
            fresh["waypoints"] = wps
            fresh["price"] = calculate_trip_price(
                float(fresh.get("km") or 0), int(fresh.get("minutes") or 0),
                pickup_city=fresh.get("pickup_city") or "",
                destination_city=fresh.get("destination_city") or "",
                waypoints_count=count_waypoints(wps),
            )
        fresh["entrance"] = route["entrance"]
        fresh["label"] = route["label"]
        fresh["step"] = 3
        return fresh
    except Exception:
        pass
    # Фолбэк: пересчёт только по тарифу (без повторного геокодинга).
    draft["price"] = calculate_trip_price(
        float(route.get("km") or 0), int(route.get("minutes") or 0),
        pickup_city=route.get("pickup_city") or "",
        destination_city=route.get("destination_city") or "",
        waypoints_count=count_waypoints(wps),
    )
    draft["is_intercity"] = _is_intercity(route.get("pickup_city"), route.get("destination_city"))
    return draft


def create_order(uid: int, draft: dict, price=None) -> dict:
    active = S.get_passenger_active_order(uid)
    if active:
        # Зависшие заказы старше 6 часов закрываем автоматически
        created = active.get("created_at") or ""
        stale = False
        try:
            if created:
                dt = datetime.fromisoformat(created)
                stale = (datetime.now() - dt).total_seconds() > 6 * 3600
        except Exception:
            stale = False
        if stale:
            S.update_order(active["id"], status="cancelled",
                           cancelled_by="timeout",
                           cancelled_at=datetime.now().isoformat())
            logger.info("Закрыт зависший заказ #%s (создан %s)", active.get("id"), created)
        else:
            raise ValueError("У вас уже есть активный заказ")
    user = S.get_user(uid) or {}
    oid = S.create_order({
        "passenger_id": uid,
        "pickup": with_entrance(draft["pickup"], draft.get("entrance")),
        "entrance": str(draft.get("entrance") or "").strip(),
        "destination": draft["destination"],
        "pickup_coords": draft.get("pickup_coords"),
        "destination_coords": draft.get("destination_coords"),
        "distance": draft.get("km") or 0,
        "time_minutes": draft.get("minutes") or 0,
        "base_price": draft.get("price") or 0,
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
    if S.commission_requires_access() and not S.has_active_access(uid):
        raise ValueError("Нет активной смены")
    if S.is_driver_blocked(uid):
        raise ValueError("Сейчас нельзя взять заказ")
    if is_driver_busy(uid):
        active = S.get_driver_active_orders(uid)
        if len(active) >= 2:
            raise ValueError("У вас уже два заказа — завершите текущий")
        raise ValueError("Сначала нажмите «Начать движение» по текущему заказу")
    order = S.get_order(oid)
    if not order or order.get("status") != "pending":
        raise ValueError("Заказ уже занят или не найден")
    S.update_order(oid, driver_id=uid, status="accepted", accepted_at=datetime.now().isoformat())
    # driver_current_order_id — это ТЕКУЩИЙ заказ (первый в очереди).
    active = S.get_driver_active_orders(uid)
    S.update_user(uid, driver_current_order_id=active[0].get("id") if active else oid)
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


def own_current_order(uid: int, oid: int) -> dict:
    """Заказ водителя, которым СЕЙЧАС можно управлять.

    Второй (следующий) принятый заказ управлению не подлежит,
    пока не завершён текущий.
    """
    order = own_order(uid, oid, as_driver=True)
    active = S.get_driver_active_orders(uid)
    if len(active) > 1 and active[0].get("id") != int(oid):
        raise ValueError("Сначала завершите текущий заказ — этот в очереди следующим")
    return order


def complete(uid: int, oid: int, lat: float | None = None, lon: float | None = None) -> dict:
    # Только ТЕКУЩИМ заказом можно управлять: следующий ждёт своей очереди.
    order = own_current_order(uid, oid)
    if order.get("status") not in ("in_progress", "driver_arrived", "accepted"):
        raise ValueError("Завершить можно поездку в пути")

    has_coords = lat is not None and lon is not None

    if has_coords:
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
    else:
        # Без гео: сохраняем уже известные расстояние, время и адрес
        address = order.get("destination") or ""
        city = order.get("destination_city") or ""
        new_distance = float(order.get("distance") or 0)
        new_duration = int(order.get("time_minutes") or 0)
    old_price = float(order.get("price") or 0)
    new_price = calculate_trip_price(
        new_distance, new_duration,
        pickup_city=order.get("pickup_city"),
        destination_city=city or order.get("destination_city"),
        waypoints_count=count_waypoints(order.get("waypoints")),
    )
    update_fields = {
        "destination": address or order.get("destination") or "",
        "destination_city": city or order.get("destination_city") or "",
        "distance": new_distance,
        "time_minutes": new_duration,
        "price": new_price,
        "original_price": old_price,
        "recalculated_at": datetime.now().isoformat(),
    }
    # Координаты высадки сохраняем только если они реально пришли
    if has_coords:
        update_fields["destination_coords"] = {"lat": lat, "lon": lon}
    S.update_order(oid, **update_fields)
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

    # Уведомляем пассажира и предлагаем сохранить маршрут в избранное
    final_order = S.get_order(oid) or {}
    _notify_passenger(
        final_order,
        f"✅ <b>Поездка #{oid} завершена</b>\n\n"
        f"📍 {final_order.get('pickup') or '—'}\n"
        f"🎯 {final_order.get('destination') or '—'}\n"
        f"📏 {new_distance:.1f} км · {new_duration} мин\n"
        f"💰 <b>Итого: {total:.0f} ₽</b>\n\n"
        "Сохранить этот маршрут в избранное, чтобы заказывать в один клик?",
        keyboard=[
            [{"type": "callback", "text": "⭐ В избранное",
              "payload": f"passenger:fav_add:{oid}"}],
            [{"type": "callback", "text": "🚖 Статус заказа",
              "payload": f"passenger:order:{oid}"}],
        ],
    )

    return brief_order(final_order)


def flag(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def act(uid: int, body: dict) -> dict:
    op = body.get("op") or ""
    if op == "login":
        # Вход по персональному токену — изолирует пользователей
        token = str(body.get("token") or "").strip()
        if token:
            uid_by_token = S.resolve_token(token)
            if not uid_by_token:
                raise ValueError("Ссылка устарела. Откройте приложение из бота заново.")
            if uid_by_token not in S.users:
                S.ensure_user(uid_by_token)
            return {"ok": True, "user_id": uid_by_token}

        user_id = int(body.get("user_id") or 0)
        if user_id not in S.users and user_id > 0:
            name = (body.get("name") or "").strip()
            S.ensure_user(user_id, name=name)
        if user_id not in S.users:
            raise ValueError("Пользователь не найден")
        return {"ok": True, "user_id": user_id}
    if op == "geo.request":
        # Мини-апп просит бота прислать кнопку гео в чат
        if not uid:
            raise PermissionError("Сначала выберите профиль")
        kind = body.get("kind") or "pickup"
        req = S.set_geo_request(uid, kind=kind, ttl_minutes=10)
        run(_geo_prompt_async(uid, kind))
        return {"ok": True, "geo": req, "bot_link": _bot_link()}

    if op == "geo.poll":
        # Мини-апп проверяет, прислал ли пользователь точку в чат
        if not uid:
            raise PermissionError("Сначала выберите профиль")
        req = S.get_geo_request(uid)
        if not req:
            return {"ok": True, "status": "none"}
        if req.get("status") == "done":
            return {"ok": True, "status": "done",
                    "lat": req.get("lat"), "lon": req.get("lon"),
                    "address": req.get("address") or "",
                    "city": req.get("city") or ""}
        return {"ok": True, "status": req.get("status") or "pending"}

    if op == "geo.cancel":
        if not uid:
            raise PermissionError("Сначала выберите профиль")
        S.clear_geo_request(uid)
        return {"ok": True}

    if op == "client.diag":
        # Диагностика окружения мини-приложения (для отладки)
        info = body.get("info") or {}
        S.admin_data.setdefault("client_diag", {})[str(uid or 0)] = {
            "at": datetime.now().isoformat(),
            "user_agent": str(info.get("userAgent") or "")[:300],
            "platform": str(info.get("platform") or "")[:40],
            "is_iframe": bool(info.get("isIframe")),
            "has_init_user": bool(info.get("hasInitUser")),
            "init_user_id": info.get("initUserId"),
            "has_bridge": bool(info.get("hasBridge")),
            "has_token": bool(info.get("hasToken")),
            "has_cookie": bool(info.get("hasCookie")),
        }
        S.save()
        logger.info("Диагностика клиента %s: %s", uid, S.admin_data["client_diag"][str(uid or 0)])
        return {"ok": True}

    if op == "session.ping":
        # Пинг для поддержания активности мини-приложения
        if not uid:
            return {"ok": True}
        u = S.get_user(uid) or {}
        if u:
            S.update_user(uid, last_active=datetime.now().isoformat())
        return {"ok": True}

    if op == "geo.suggest":
        # Подсказки адреса при вводе (автодополнение)
        query = str(body.get("query") or "").strip()
        if len(query) < 2:
            return {"ok": True, "items": []}
        city = str(body.get("city") or "").strip()
        if not city and uid:
            u = S.get_user(uid) or {}
            city = u.get("home_city") or u.get("current_city") or ""
        lat = body.get("lat")
        lon = body.get("lon")
        try:
            lat = float(lat) if lat is not None else None
            lon = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        items = run(geocoder.suggest_address(
            query, city=city, limit=int(body.get("limit") or 8),
            near_lat=lat, near_lon=lon,
        ))
        return {"ok": True, "items": items}

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
            fields["name"] = str(body["name"]).strip()[:60]
        if body.get("phone"):
            fields["phone"] = _normalize_phone(str(body["phone"]))
        if body.get("home_city"):
            fields["home_city"] = str(body["home_city"]).strip()[:60]
        if fields:
            S.update_user(uid, **fields)
        u = S.get_user(uid) or {}
        # Если профиль заполнен — знакомство больше не показываем
        if _profile_complete(u):
            S.mark_onboarded(uid)
            u = S.get_user(uid) or {}
        return {"ok": True, "profile_complete": _profile_complete(u),
                "needs_onboarding": _needs_onboarding(u)}
    if op == "quote":
        return {"ok": True, "draft": quote(
            body.get("pickup", ""), body.get("destination", ""),
            body.get("stop", ""),
            pickup_coords=body.get("pickup_coords"),
            dest_coords=body.get("dest_coords") or body.get("destination_coords"),
            pickup_city=body.get("pickup_city", ""),
            destination_city=body.get("destination_city", ""),
        )}
    if op == "order.create":
        # Заказ нельзя создать, пока не пройдено знакомство
        u = S.get_user(uid) or {}
        if _needs_onboarding(u):
            raise ValueError("Заполните профиль: имя, телефон и город")
        draft = body.get("draft") or {}
        # ВАЖНО: цена всегда пересчитывается по АКТУАЛЬНОМУ тарифу.
        # Если заказ создаётся из избранного, в draft лежит старая цена —
        # тарифы могли измениться, поэтому делаем quote заново.
        if body.get("from_favorite") or draft.get("favoriteIndex") is not None \
                or not draft.get("pickup_coords"):
            fresh = quote(
                draft.get("pickup", ""), draft.get("destination", ""),
                draft.get("stop", ""),
                pickup_coords=draft.get("pickup_coords"),
                dest_coords=draft.get("dest_coords") or draft.get("destination_coords"),
                pickup_city=draft.get("pickup_city", ""),
                destination_city=draft.get("destination_city", ""),
            )
            # Сохраняем служебные поля из черновика
            for key in ("entrance", "favoriteIndex", "label"):
                if draft.get(key) is not None:
                    fresh[key] = draft[key]
            draft = fresh
        price = body.get("price")
        if price is not None:
            try:
                price = int(price)
            except (TypeError, ValueError):
                raise ValueError("Некорректная сумма")
            calc = int(draft.get("price") or 0)
            if price < calc:
                raise ValueError(f"Своя цена не может быть ниже рассчитанной ({calc} ₽)")
            if price < 50:
                raise ValueError("Сумма слишком маленькая")
        return {"ok": True, "order": create_order(uid, draft, price)}
    if op == "order.nav":
        # Навигация: строим маршрут со ВСЕМИ промежуточными точками
        # (подача → остановки → назначение), а не просто «А → Б».
        order = S.get_order(int(body.get("id") or 0))
        if not order or uid not in (order.get("passenger_id"), order.get("driver_id")):
            raise ValueError("Нет доступа к заказу")
        phase = str(body.get("phase") or "").strip()
        if phase not in ("to_pickup", "in_trip"):
            phase = ("in_trip" if order.get("status") in
                     ("in_progress", "completed") else "to_pickup")
        start = None
        if uid == order.get("driver_id"):
            loc = (S.get_user(uid) or {}).get("last_location") or {}
            if isinstance(loc, dict):
                try:
                    start = (float(loc["lat"]), float(loc["lon"]))
                except (KeyError, TypeError, ValueError):
                    start = None
        url = generate_navigation_url(order, phase=phase, start=start)
        google = generate_google_navigation_url(order, phase=phase, start=start)
        stops = [str(w.get("text") or w.get("address") or "")
                 for w in (order.get("waypoints") or []) if isinstance(w, dict)]
        return {"ok": True, "url": url, "google": google, "phase": phase,
                "stops": [s for s in stops if s]}
    if op == "order.nav":
        # Ссылка навигатора со ВСЕМИ точками маршрута:
        # к подаче — [машина] → подача; в пути — подача → остановки → назначение.
        order = S.get_order(int(body.get("id") or 0))
        if not order or uid not in (order.get("passenger_id"), order.get("driver_id")):
            raise ValueError("Нет доступа к заказу")
        phase = str(body.get("phase") or "").strip()
        if not phase:
            phase = ("in_trip" if order.get("status") == "in_progress"
                     else "to_pickup")
        start = None
        if uid == order.get("driver_id"):
            loc = (S.get_user(uid) or {}).get("last_location") or {}
            if isinstance(loc, dict):
                try:
                    start = (float(loc["lat"]), float(loc["lon"]))
                except (KeyError, TypeError, ValueError):
                    start = None
        return {
            "ok": True,
            "phase": phase,
            "url": generate_navigation_url(order, phase=phase, start=start),
            "google": generate_google_navigation_url(order, phase=phase, start=start),
        }
    if op == "order.route":
        order = S.get_order(int(body.get("id") or 0))
        if not order or uid not in (order.get("passenger_id"), order.get("driver_id")):
            raise ValueError("Нет доступа к заказу")
        if order.get("status") not in ("pending", "accepted", "driver_arrived", "in_progress"):
            raise ValueError("Маршрут этого заказа уже нельзя менять")
        # ВОДИТЕЛЬ НЕ может менять адрес/остановки вообще: маршрут меняет
        # только пассажир. Даже пока водитель едет к подаче — нельзя.
        if uid == order.get("driver_id"):
            raise ValueError("Адрес меняет только пассажир")
        dest = (body.get("destination") or order.get("destination") or "").strip()
        stop = (body.get("stop") or "").strip()
        if len(dest) < 3:
            raise ValueError("Укажите адрес назначения")
        fresh = quote(
            order.get("pickup") or "", dest, stop,
            pickup_coords=order.get("pickup_coords"),
            dest_coords=body.get("destination_coords") or body.get("dest_coords"),
            pickup_city=order.get("pickup_city") or "",
            destination_city=body.get("destination_city") or "",
        )
        S.update_order(
            order["id"],
            destination=fresh["destination"],
            destination_coords=fresh.get("destination_coords"),
            destination_city=fresh.get("destination_city"),
            waypoints=fresh.get("waypoints") or [],
            distance=fresh.get("km") or 0,
            time_minutes=fresh.get("minutes") or 0,
            base_price=fresh["price"],
            price=fresh["price"],
        )
        text = f"Маршрут заказа #{order['id']} изменён: {fresh['destination']}"
        if uid == order.get("passenger_id") and order.get("driver_id"):
            _notify_passenger({"passenger_id": order.get("driver_id")}, text)
        elif uid == order.get("driver_id"):
            _notify_passenger(order, text)
        return {"ok": True}
    if op == "order.price":
        order = own_order(uid, int(body["id"]))
        if order.get("status") not in ("pending", "accepted", "driver_arrived"):
            raise ValueError("Цену уже нельзя менять")
        price = int(body.get("price") or 0)
        if price < 50:
            raise ValueError("Сумма слишком маленькая")
        base = int(order.get("base_price") or order.get("price") or 0)
        if price <= base:
            raise ValueError(f"Своя цена должна быть больше рассчитанной ({base} ₽)")
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
    if op == "order.wait":
        # Пассажир ответил «ждать ещё» — авто-отмена не сработает.
        order = own_order(uid, int(body["id"]))
        if order.get("status") != "pending":
            raise ValueError("Заказ уже не ищет водителя")
        S.set_passenger_waiting(order["id"], True)
        return {"ok": True, "order": brief_order(S.get_order(order["id"]) or order)}
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
    if op == "order.call":
        order = S.get_order(int(body.get("id") or 0))
        if not order or uid not in (order.get("passenger_id"), order.get("driver_id")):
            raise ValueError("Нет доступа к заказу")
        if uid == order.get("passenger_id"):
            peer = order.get("driver_id")
            peer_u = S.get_user(peer) or {}
            name = peer_u.get("driver_name") or peer_u.get("name") or "Водитель"
            phone = peer_u.get("phone") or ""
            who = "водителя"
        else:
            peer = order.get("passenger_id")
            peer_u = S.get_user(peer) or {}
            name = peer_u.get("name") or "Пассажир"
            phone = peer_u.get("phone") or ""
            who = "пассажира"
        if not peer:
            raise ValueError("Собеседник ещё не назначен")
        _send_call_card(uid, int(peer), name, phone, who)
        return {"ok": True, "bot_link": _bot_link()}
    if op == "order.chat":
        try:
            oid = int(body.get("id") or 0)
        except (TypeError, ValueError):
            oid = 0
        if not oid:
            raise ValueError("Не указан заказ")
        order = S.get_order(oid)
        if not order or uid not in (order.get("passenger_id"), order.get("driver_id")):
            raise ValueError("Нет доступа к переписке")
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError("Пустое сообщение")
        chat = list(order.get("chat") or [])
        chat.append({"from": uid, "text": text, "at": datetime.now().isoformat()})
        S.update_order(order["id"], chat=chat[-40:])

        # Уведомляем собеседника в MAX
        pid = order.get("passenger_id")
        did = order.get("driver_id")
        peer = did if uid == pid else pid
        if peer:
            sender_role = "Водитель" if uid == did else "Пассажир"
            open_payload = (
                f"passenger:order:{order['id']}" if uid == did
                else "driver:current_order"
            )
            try:
                peer_int = int(peer)
                import threading

                def worker():
                    try:
                        asyncio.run(_notify_async(
                            peer_int,
                            f"💬 <b>{sender_role}</b> · заказ #{order['id']}\n\n"
                            f"{text[:300]}",
                            keyboard=[[{
                                "type": "callback",
                                "text": "Открыть заказ",
                                "payload": open_payload,
                            }]],
                        ))
                    except Exception as exc:
                        logger.warning("Уведомление о сообщении %s: %s", peer_int, exc)

                threading.Thread(target=worker, daemon=True).start()
            except (TypeError, ValueError):
                pass

        return {"ok": True, "chat": chat[-40:]}
    if op == "favorite.add":
        order = own_order(uid, int(body["id"]))
        default_label = f"{order.get('pickup', '')} → {order.get('destination', '')}"
        # Пассажир может дать своё название («На работу», «В школу» и т.п.)
        label = str(body.get("label") or "").strip() or default_label
        S.add_favorite(uid, order["id"], label=label[:80])
        return {"ok": True, "label": label[:80]}
    if op == "favorite.edit":
        pickup = (body.get("pickup") or "").strip()
        destination = (body.get("destination") or "").strip()
        if len(pickup) < 3 or len(destination) < 3:
            raise ValueError("Укажите адрес подачи и назначения")
        label = (body.get("label") or "").strip() or f"{pickup} → {destination}"
        S.update_favorite(
            uid, int(body.get("index") or 0),
            pickup=pickup, destination=destination,
            entrance=(body.get("entrance") or "").strip(),
            label=label[:80],
        )
        return {"ok": True}
    if op == "favorite.preview":
        return {"ok": True, "draft": _favorite_draft(uid, int(body.get("index") or 0))}
    if op == "favorite.use":
        draft = _favorite_draft(uid, int(body.get("index") or 0))
        return {"ok": True, "order": create_order(uid, draft)}
    if op == "favorite.remove":
        idx = int(body.get("index") if body.get("index") is not None else -1)
        if idx < 0:
            oid = int(body.get("id") or 0)
            ok = S.remove_favorite(uid, oid) if oid else False
        else:
            ok = S.remove_favorite_at(uid, idx)
        if not ok:
            raise ValueError("Избранное не найдено")
        return {"ok": True}
    if op == "driver.apply":
        number = (body.get("plate") or "").strip()
        color = (body.get("color") or "").strip()
        model = (body.get("car") or "").strip()
        if len(number.replace(" ", "")) < 5 or len(color) < 2 or len(model) < 2:
            raise ValueError("Нужны марка, номер и цвет")
        # Фото авто обязательно: его присылают в чат с ботом (car_photo_token)
        # либо загружают в Mini-App (car_photo_path через /api/photo).
        cur = S.get_user(uid) or {}
        has_photo = bool(
            (cur.get("car_photo_path") or "").strip()
            or (cur.get("car_photo_token") or "").strip()
        )
        photo_key = (body.get("photo") or "").strip()
        if not has_photo and not photo_key:
            raise ValueError("Прикрепите фото автомобиля — это обязательно для регистрации")
        S.update_user(uid, car_model=model, driver_name=S.get_passenger_name(uid) or model)
        S.submit_driver_application(
            uid, car_number=number, car_color=color, car_model=model,
            car_photo_token=cur.get("car_photo_token"),
            car_photo_path=cur.get("car_photo_path") or (photo_key or None),
        )
        return {"ok": True}
    if op == "driver.line":
        must_role(uid, "driver")
        status = body.get("status")
        if status not in ("online", "offline", "break"):
            raise ValueError("Неизвестный статус")
        if S.is_driver_blocked(uid):
            raise ValueError("Профиль временно заблокирован")
        if status == "online" and S.commission_requires_access() and not S.has_active_access(uid):
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
        result = accept(uid, int(body["id"]))
        order = S.get_order(int(body["id"])) or {}
        _notify_passenger(
            order,
            f"🚗 <b>Ваш заказ #{order.get('id')} принят!</b>\n\n"
            f"👤 Водитель: {result.get('driver_name') or '—'}\n"
            + (f"🚘 {result.get('car_model') or ''} {result.get('car_number') or ''}\n"
               if result.get('car_number') else "")
            + (f"🎨 {result.get('car_color')}\n" if result.get('car_color') else "")
            + f"\n📍 Подача: {order.get('pickup') or '—'}\n"
            f"💰 Стоимость: {order.get('price') or 0} ₽\n\n"
            "Водитель уже едет к вам.",
            keyboard=[[{"type": "callback", "text": f"🚖 Статус заказа",
                        "payload": f"passenger:order:{order.get('id')}"}]],
        )
        return {"ok": True, "order": result}

    if op == "driver.arrived":
        order = own_current_order(uid, int(body["id"]))
        if order.get("status") != "accepted":
            raise ValueError("Сначала заказ должен быть принят")
        now = datetime.now().isoformat()
        # «Прибыл» сразу включает таймер ожидания — отдельная кнопка
        # «Ожидание» больше не нужна: отсчёт идёт у водителя и пассажира.
        S.update_order(order["id"], status="driver_arrived", driver_arrived_at=now,
                       waiting_started_at=now, waiting_ended_at=None,
                       waiting_seconds_total=0)
        fresh = S.get_order(order["id"]) or order
        info = _waiting_payload(fresh)
        _notify_passenger(
            fresh,
            f"📍 <b>Водитель прибыл</b> по заказу #{order['id']}\n\n"
            f"Машина: {(S.get_user(uid) or {}).get('car_model') or ''} "
            f"{(S.get_user(uid) or {}).get('car_number') or ''}\n\n"
            f"⏳ Ожидание пошло. Бесплатно {int(info['free_minutes'])} мин, "
            f"далее {info['per_minute']:.0f} ₽/мин.",
        )
        return {"ok": True, "waiting": info}

    if op == "driver.wait":
        # Кнопка «Ожидание» в пути: только старт/стоп таймера.
        # СТАТУС ЗАКАЗА НЕ МЕНЯЕТСЯ НИКОГДА — иначе в интерфейсе
        # «Завершить поездку» превращается в «Начать движение».
        order = own_current_order(uid, int(body["id"]))
        status_before = order.get("status")
        if status_before not in ("in_progress", "driver_arrived"):
            raise ValueError("Ожидание доступно только во время поездки")

        running = bool(order.get("waiting_started_at") and not order.get("waiting_ended_at"))
        if running:
            stop_waiting(order["id"])
        else:
            now = datetime.now().isoformat()
            S.update_order(order["id"], waiting_started_at=now, waiting_ended_at=None)

        fresh = S.get_order(order["id"]) or order
        # Защита: если по какой-то причине статус «съехал» — возвращаем.
        if fresh.get("status") != status_before:
            S.update_order(order["id"], status=status_before)
            fresh = S.get_order(order["id"]) or fresh
        return {"ok": True, "waiting": _waiting_payload(fresh),
                "order": brief_order(fresh)}

    if op == "driver.start":
        order = own_current_order(uid, int(body["id"]))
        # Идемпотентность: если поездка уже идёт — НЕ трогаем ожидание
        # и НЕ сбрасываем статус. Иначе повторное нажатие «Начать движение»
        # (например, со старой кэшированной версией интерфейса) останавливает
        # платное ожидание.
        if order.get("status") == "in_progress":
            return {"ok": True, "already": True, "order": brief_order(order)}
        if order.get("status") not in ("accepted", "driver_arrived"):
            raise ValueError("Поездку сейчас нельзя начать")
        stop_waiting(order["id"])
        S.update_order(order["id"], status="in_progress", started_at=datetime.now().isoformat())
        _notify_passenger(
            S.get_order(order["id"]) or order,
            f"🚗 <b>Поездка #{order['id']} началась</b>\n\n"
            f"🎯 Едем: {order.get('destination') or '—'}\n"
            f"📏 {order.get('distance') or 0} км · ~{order.get('time_minutes') or 0} мин\n"
            f"💰 {order.get('price') or 0} ₽\n\n"
            "Хорошей поездки!",
        )
        return {"ok": True}
    if op == "driver.complete":
        # Координаты необязательны: без гео завершаем по сохранённому адресу
        lat = body.get("lat")
        lon = body.get("lon")
        try:
            lat = float(lat) if lat is not None else None
            lon = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        return {"ok": True, "order": complete(uid, int(body["id"]), lat, lon)}
    if op == "driver.day":
        # Полный список завершённых заявок за выбранный день — для графика
        # заработка в Mini-App (не только последние 30 из ленты истории).
        must_role(uid, "driver")
        date = (body.get("date") or "").strip()
        if len(date) != 10 or ":" in date:
            raise ValueError("Укажите дату YYYY-MM-DD")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Укажите дату YYYY-MM-DD")
        orders = S.get_driver_history(uid, 1000, period=f"date:{date}")
        total = 0.0
        earning_total = 0.0
        count = len(orders)
        for o in orders:
            paid = _order_total(o)
            total += paid
            earned = o.get("driver_earning")
            if earned in (None, ""):
                earned = paid
            try:
                earning_total += float(earned)
            except (TypeError, ValueError):
                pass
        return {"ok": True, "date": date, "sum": round(total, 2),
                "earning": round(earning_total, 2), "count": count,
                "orders": [brief_order(o) for o in orders]}
    if op == "driver.period":
        # Все завершённые заявки за период (день/неделя/месяц) — для кнопки
        # «Заявки за период» под графиком заработка.
        must_role(uid, "driver")
        period = (body.get("period") or "week").strip()
        if period not in ("day", "week", "month"):
            raise ValueError("Неизвестный период")
        days = {"day": 1, "week": 7, "month": 30}[period]
        orders = S.get_driver_history(uid, 1000, period=f"days:{days}")
        total = 0.0
        earning_total = 0.0
        for o in orders:
            paid = _order_total(o)
            total += paid
            earned = o.get("driver_earning")
            if earned in (None, ""):
                earned = paid
            try:
                earning_total += float(earned)
            except (TypeError, ValueError):
                pass
        return {"ok": True, "period": period, "days": days,
                "sum": round(total, 2), "earning": round(earning_total, 2),
                "count": len(orders),
                "orders": [brief_order(o) for o in orders]}
    if op == "driver.cancel":
        order = own_current_order(uid, int(body["id"]))
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
        return admin_act(body, actor=uid)
    raise ValueError("Неизвестное действие")


_TARIFF_FIELDS = (
    "base_price", "included_km", "price_per_km", "price_per_minute",
    "min_price", "rounding_step", "free_waiting_minutes", "waiting_price_per_minute",
    "day_surcharge_percent", "night_surcharge_percent",
    "night_start_hour", "night_end_hour", "waypoint_surcharge",
)


def _tariff_key(tariffs: dict, city: str) -> str:
    if city in tariffs:
        return city
    for key in tariffs:
        if normalize_city(str(key)) == city:
            return key
    return city


def _apply_tariff_fields(current: dict, body: dict) -> None:
    for field in _TARIFF_FIELDS:
        raw = body.get(field)
        if raw is None or raw == "":
            continue
        current[field] = float(raw)


def admin_act(body: dict, actor: int | None = None) -> dict:
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
        target = int(body["id"])
        if actor and target == int(actor):
            raise ValueError("Нельзя удалить свою учётную запись")
        if not S.delete_user(target):
            raise ValueError("Пользователь не найден")
    elif op == "admin.user.edit":
        target = int(body["id"])
        payload = {}
        for key in ("name", "phone", "car_model", "car_number", "car_color"):
            if key in body and body.get(key) is not None:
                payload[key] = body.get(key)
        S.edit_user_profile(target, **payload)
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
        city = normalize_city(body.get("city") or "")
        if not city:
            raise ValueError("Укажите город")
        tariffs = dict(S.admin_data.get("tariffs_by_city") or {})
        current = dict(tariffs.pop(_tariff_key(tariffs, city), {}) or {})
        _apply_tariff_fields(current, body)
        if not any(field in current for field in _TARIFF_FIELDS):
            raise ValueError("Укажите хотя бы одно значение тарифа")
        tariffs[city] = current
        S.admin_data["tariffs_by_city"] = tariffs
        S.save()
    elif op == "admin.tariff.delete":
        city = normalize_city(body.get("city") or "")
        if not city:
            raise ValueError("Укажите город")
        tariffs = dict(S.admin_data.get("tariffs_by_city") or {})
        key = _tariff_key(tariffs, city)
        if key not in tariffs:
            raise ValueError("Тариф этого города не найден")
        tariffs.pop(key, None)
        S.admin_data["tariffs_by_city"] = tariffs
        S.save()
    elif op == "admin.tariff.intercity":
        current = dict(S.admin_data.get("tariff_intercity") or {})
        _apply_tariff_fields(current, body)
        if not current:
            raise ValueError("Укажите хотя бы одно значение тарифа")
        S.admin_data["tariff_intercity"] = current
        S.save()
    elif op == "admin.tariff.waiting":
        # Настройки ожидания: бесплатные минуты и цена за минуту.
        # Значения общие (межгород-тариф) + можно задать для города.
        city = normalize_city(body.get("city") or "")
        target = None
        if city:
            tariffs = dict(S.admin_data.get("tariffs_by_city") or {})
            key = _tariff_key(tariffs, city)
            current = dict(tariffs.get(key) or {})
            fields = {}
            if body.get("free_minutes") is not None:
                fields["free_waiting_minutes"] = max(0.0, float(body["free_minutes"]))
            if body.get("per_minute") is not None:
                fields["waiting_price_per_minute"] = max(0.0, float(body["per_minute"]))
            if not fields:
                raise ValueError("Укажите бесплатные минуты или цену за минуту")
            current.update(fields)
            tariffs[key] = current
            S.admin_data["tariffs_by_city"] = tariffs
            target = "city"
        else:
            current = dict(S.admin_data.get("tariff_intercity") or {})
            if body.get("free_minutes") is not None:
                current["free_waiting_minutes"] = max(0.0, float(body["free_minutes"]))
            if body.get("per_minute") is not None:
                current["waiting_price_per_minute"] = max(0.0, float(body["per_minute"]))
            if not current:
                raise ValueError("Укажите бесплатные минуты или цену за минуту")
            S.admin_data["tariff_intercity"] = current
            target = "intercity"
        S.save()
        logger.info("Ожидание обновлено (%s): %s", target, body)
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
            if path == "/api/whoami":
                # Кого сервер видит. Без глобального «последний пользователь» —
                # только cookie или персональный токен.
                uid = uid_from(self)
                self.send_json(200, {"ok": True, "uid": uid,
                                     "source": "cookie" if uid else "none"})
                return
            if path == "/api/state":
                uid = uid_from(self)
                if not uid:
                    # Нет сессии — отдаём пустой ответ вместо 401,
                    # чтобы приложение показало выбор профиля, а не падало
                    self.send_json(200, {"ok": False, "error": "Нет сессии"})
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

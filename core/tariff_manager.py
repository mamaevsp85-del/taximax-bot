"""
Менеджер тарифов и расчёт стоимости поездки.

Логика портирована из services/tariff_manager.py (без изменений формулы).
Тарифы берутся из core.storage.storage.admin_data:
  - admin_data['tariff_intercity']      — межгород
  - admin_data['tariffs_by_city'][city] — по городам

Дополнительно:
  - day_surcharge_percent / night_surcharge_percent — % наценка день/ночь
  - night_start_hour / night_end_hour — границы ночи (по умолчанию 22–6)
  - waypoint_surcharge — фикс. ₽ за каждую промежуточную точку маршрута
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional

from core import storage
from core.service_area import normalize_city

logger = logging.getLogger(__name__)


def _default_tariff() -> Dict[str, Any]:
    """Городской тариф по умолчанию."""
    return {
        "base_price": 100, "included_km": 2, "price_per_km": 20,
        "price_per_minute": 2, "min_price": 50,
        "rounding_type": "up", "rounding_step": 10,
        "free_waiting_minutes": 3, "waiting_price_per_minute": 5,
        "day_surcharge_percent": 0, "night_surcharge_percent": 0,
        "night_start_hour": 22, "night_end_hour": 6,
        "waypoint_surcharge": 0,
    }


def _default_intercity_tariff() -> Dict[str, Any]:
    """Междугородний тариф по умолчанию — дороже городского.

    Время учитывается: заезд в город (пробки, светофоры) должен
    отражаться в цене, даже если километраж почти не вырос.
    """
    return {
        "base_price": 150, "included_km": 5, "price_per_km": 35,
        "price_per_minute": 3, "min_price": 300,
        "rounding_type": "up", "rounding_step": 10,
        "free_waiting_minutes": 5, "waiting_price_per_minute": 5,
        "day_surcharge_percent": 0, "night_surcharge_percent": 0,
        "night_start_hour": 22, "night_end_hour": 6,
        "waypoint_surcharge": 0,
    }


def _to_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def get_tariff_settings(city: Optional[str] = None, intercity: bool = False) -> Dict[str, Any]:
    admin = storage.storage.admin_data
    city_key = normalize_city(city or "")
    if intercity:
        saved = admin.get("tariff_intercity") or {}
        base = _default_intercity_tariff()
        base.update({k: v for k, v in saved.items() if v is not None})
        return base
    if city_key:
        tb = admin.get("tariffs_by_city") or {}
        city_tariff = tb.get(city_key) or {}
        if not city_tariff:
            for key, value in tb.items():
                if normalize_city(key) == city_key:
                    city_tariff = value or {}
                    break
        base = _default_tariff()
        base.update({k: v for k, v in city_tariff.items() if v is not None})
        return base
    return _default_tariff()


def apply_rounding(price: float, tariff: Dict[str, Any]) -> float:
    rounding_type = tariff.get("rounding_type", "up")
    step = _to_number(tariff.get("rounding_step", 10), 10)
    if step <= 0:
        return price
    if rounding_type == "up":
        return math.ceil(price / step) * step
    elif rounding_type == "down":
        return math.floor(price / step) * step
    return round(price / step) * step


def _is_intercity(pickup_city: Optional[str], destination_city: Optional[str]) -> bool:
    """Межгород — только если ОБА города известны и различаются.

    Если город назначения не определён, поездка считается внутригородской:
    пассажир заказывает такси из своего города, и адрес без города
    относится к тому же городу.
    """
    if not pickup_city or not destination_city:
        return False
    return normalize_city(pickup_city) != normalize_city(destination_city)


def resolve_destination_city(pickup_city: Optional[str],
                             destination_city: Optional[str],
                             destination_text: Optional[str] = "",
                             distance_km: Optional[float] = None) -> str:
    """Город назначения для выбора тарифа.

    Межгород — когда оба города известны и они разные.
    Исключение: геокодер нашёл одноимённую улицу в другом регионе
    (сотни километров) и пассажир этот город в адресе не писал —
    тогда оставляем город подачи.
    """
    if not pickup_city:
        return destination_city or ""
    if not destination_city:
        return pickup_city
    if normalize_city(pickup_city) == normalize_city(destination_city):
        return destination_city

    named = normalize_city(destination_city) in (destination_text or "").lower()
    if distance_km is not None and float(distance_km) > 300 and not named:
        return pickup_city
    return destination_city


def is_intercity(pickup_city: Optional[str], destination_city: Optional[str]) -> bool:
    """Является ли поездка межгородней (разные города подачи и назначения)."""
    return _is_intercity(pickup_city, destination_city)


def is_night_time(at: Optional[datetime] = None, *,
                  night_start: int = 22, night_end: int = 6) -> bool:
    """Ночь: [night_start .. 24) U [0 .. night_end). День — остальное.

    Особые случаи:
      * night_end = 24 — ночь длится с night_start до полуночи (не «нет ночи»);
      * night_start == night_end — ночи нет (весь день считается днём).
    """
    hour = (at or datetime.now()).hour
    raw_start, raw_end = int(night_start), int(night_end)
    if raw_end >= 24:
        return hour >= (raw_start % 24)
    start = raw_start % 24
    end = raw_end % 24
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def count_waypoints(waypoints: Any) -> int:
    """Число промежуточных точек (остановок) в маршруте."""
    if not waypoints:
        return 0
    if isinstance(waypoints, (list, tuple)):
        return len([w for w in waypoints if w])
    return 0


def _time_surcharge_percent(tariff: Dict[str, Any], at: Optional[datetime] = None) -> float:
    night = is_night_time(
        at,
        night_start=int(_to_number(tariff.get("night_start_hour", 22), 22)),
        night_end=int(_to_number(tariff.get("night_end_hour", 6), 6)),
    )
    if night:
        return max(0.0, _to_number(tariff.get("night_surcharge_percent", 0), 0))
    return max(0.0, _to_number(tariff.get("day_surcharge_percent", 0), 0))


def _raw_trip_price(distance: float, duration: int, tariff: Dict[str, Any]) -> float:
    base_price = _to_number(tariff.get("base_price", 100), 100)
    included_km = _to_number(tariff.get("included_km", 2), 2)
    price_per_km = _to_number(tariff.get("price_per_km", 20), 20)
    price_per_minute = _to_number(tariff.get("price_per_minute", 2), 2)

    if distance <= included_km:
        trip_price = base_price
    else:
        trip_price = base_price + (distance - included_km) * price_per_km
    if price_per_minute > 0:
        trip_price += float(duration) * price_per_minute
    return trip_price


def calculate_trip_price(distance: float, duration: int, *,
                         pickup_city: Optional[str] = None,
                         destination_city: Optional[str] = None,
                         waypoints_count: int = 0,
                         at: Optional[datetime] = None) -> int:
    """Рассчитать стоимость поездки (округлённую).

    Формула: тариф по маршруту + % день/ночь + ₽ × число промежуточных точек.
    """
    intercity = _is_intercity(pickup_city, destination_city)
    tariff = get_tariff_settings(city=pickup_city, intercity=intercity)
    min_price = _to_number(tariff.get("min_price", 50), 50)

    trip_price = _raw_trip_price(distance, duration, tariff)
    pct = _time_surcharge_percent(tariff, at)
    if pct > 0:
        trip_price *= (1.0 + pct / 100.0)

    stops = max(0, int(waypoints_count or 0))
    stop_fee = max(0.0, _to_number(tariff.get("waypoint_surcharge", 0), 0))
    trip_price += stops * stop_fee

    price = max(trip_price, min_price)
    price = apply_rounding(price, tariff)
    return int(price)


def calculate_waiting_price(waiting_minutes: float, *,
                            city: Optional[str] = None,
                            intercity: bool = False) -> float:
    tariff = get_tariff_settings(city=city, intercity=intercity)
    free = _to_number(tariff.get("free_waiting_minutes", 3), 3)
    per_min = _to_number(tariff.get("waiting_price_per_minute", 5), 5)
    billable = max(0.0, float(waiting_minutes) - free)
    return billable * per_min


def calculate_trip_breakdown(distance: float, duration: int, waiting_minutes: float = 0, *,
                             pickup_city: Optional[str] = None,
                             destination_city: Optional[str] = None,
                             waypoints_count: int = 0,
                             at: Optional[datetime] = None) -> Dict[str, Any]:
    """Подробный расчёт для показа пользователю."""
    intercity = _is_intercity(pickup_city, destination_city)
    tariff = get_tariff_settings(city=pickup_city, intercity=intercity)
    base_price = _to_number(tariff.get("base_price", 100), 100)
    included_km = _to_number(tariff.get("included_km", 2), 2)
    price_per_km = _to_number(tariff.get("price_per_km", 20), 20)
    price_per_minute = _to_number(tariff.get("price_per_minute", 2), 2)

    trip_price = _raw_trip_price(distance, duration, tariff)
    pct = _time_surcharge_percent(tariff, at)
    time_surcharge = trip_price * pct / 100.0 if pct > 0 else 0.0
    stops = max(0, int(waypoints_count or 0))
    stop_fee = max(0.0, _to_number(tariff.get("waypoint_surcharge", 0), 0))
    waypoints_cost = stops * stop_fee

    waiting_cost = calculate_waiting_price(waiting_minutes, city=pickup_city, intercity=intercity)
    total = int(apply_rounding(trip_price + time_surcharge + waypoints_cost + waiting_cost, tariff))

    return {
        "base_price": base_price,
        "included_km": included_km,
        "extra_km": max(0, distance - included_km),
        "extra_km_cost": max(0, (distance - included_km) * price_per_km),
        "duration_cost": duration * price_per_minute if price_per_minute > 0 else 0,
        "trip_price": int(trip_price),
        "time_surcharge_percent": pct,
        "time_surcharge": round(time_surcharge, 2),
        "is_night": is_night_time(
            at,
            night_start=int(_to_number(tariff.get("night_start_hour", 22), 22)),
            night_end=int(_to_number(tariff.get("night_end_hour", 6), 6)),
        ),
        "waypoints_count": stops,
        "waypoint_surcharge": stop_fee,
        "waypoints_cost": waypoints_cost,
        "waiting_minutes": waiting_minutes,
        "free_waiting_minutes": tariff.get("free_waiting_minutes", 3),
        "waiting_cost": waiting_cost,
        "total_cost": total,
        "is_intercity": intercity,
    }


def update_tariff_setting(setting_name: str, value: Any) -> bool:
    """Обновить настройку тарифа в текущей области редактирования."""
    try:
        admin = storage.storage.admin_data
        scope = admin.get("tariff_edit_scope") or "intercity"
        if scope == "intercity":
            admin.setdefault("tariff_intercity", {})[setting_name] = value
        elif scope.startswith("city:"):
            city_key = scope.split(":", 1)[1]
            tb = admin.setdefault("tariffs_by_city", {})
            tb.setdefault(city_key, {})[setting_name] = value
        else:
            admin.setdefault("tariff_intercity", {})[setting_name] = value
        storage.storage.save()
        return True
    except Exception as exc:
        logger.error("Ошибка обновления тарифа %s: %s", setting_name, exc)
        return False


def get_tariff_display_text() -> str:
    """Текст текущего тарифа для отображения в админке."""
    admin = storage.storage.admin_data
    scope = admin.get("tariff_edit_scope") or "intercity"
    if scope == "intercity":
        title = "Межгород"
        tariff = get_tariff_settings(intercity=True)
    elif scope.startswith("city:"):
        city_key = scope.split(":", 1)[1]
        title = f"Город: {city_key}"
        tariff = get_tariff_settings(city=city_key)
    else:
        title = "Межгород"
        tariff = get_tariff_settings(intercity=True)

    night_start = int(_to_number(tariff.get("night_start_hour", 22), 22))
    night_end = int(_to_number(tariff.get("night_end_hour", 6), 6))
    return (
        f"💰 <b>{title}</b>\n\n"
        f"🚕 Базовая стоимость: {tariff.get('base_price', 100)} ₽\n"
        f"📏 Включено км: {tariff.get('included_km', 2)} км\n"
        f"💵 Доплата за км: {tariff.get('price_per_km', 20)} ₽/км\n"
        f"⏰ Стоимость за минуту: {tariff.get('price_per_minute', 2)} ₽/мин\n"
        f"💳 Минимальная стоимость: {tariff.get('min_price', 50)} ₽\n"
        f"🔢 Округление: {tariff.get('rounding_type', 'up')} до {tariff.get('rounding_step', 10)} ₽\n"
        f"⏳ Бесплатное ожидание: {tariff.get('free_waiting_minutes', 3)} мин\n"
        f"💰 Плата за ожидание: {tariff.get('waiting_price_per_minute', 5)} ₽/мин\n"
        f"☀️ Дневная наценка: {tariff.get('day_surcharge_percent', 0)} %\n"
        f"🌙 Ночная наценка: {tariff.get('night_surcharge_percent', 0)} % "
        f"(ночь {night_start}:00–{night_end}:00)\n"
        f"📍 Наценка за промежуточную точку: {tariff.get('waypoint_surcharge', 0)} ₽"
    )

"""
Менеджер тарифов и расчёт стоимости поездки.

Логика портирована из services/tariff_manager.py (без изменений формулы).
Тарифы берутся из core.storage.storage.admin_data:
  - admin_data['tariff_intercity']      — межгород
  - admin_data['tariffs_by_city'][city] — по городам
"""
from __future__ import annotations

import logging
import math
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
    }


def _default_intercity_tariff() -> Dict[str, Any]:
    """Междугородний тариф по умолчанию — дороже городского."""
    return {
        "base_price": 150, "included_km": 5, "price_per_km": 35,
        "price_per_minute": 0, "min_price": 300,
        "rounding_type": "up", "rounding_step": 10,
        "free_waiting_minutes": 5, "waiting_price_per_minute": 5,
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
                             destination_text: Optional[str] = "") -> str:
    """Определяет город назначения без ложного межгорода.

    Проблема: пассажир пишет «Юбилейная 29» без города, а геокодер может
    найти такую улицу в другом городе — и поездка ошибочно становится
    межгородней с междугородним тарифом.

    Правило:
      • пассажир явно указал другой город в тексте адреса — межгород;
      • город не указан или совпадает с городом подачи — внутригородская.
    """
    if not pickup_city:
        return destination_city or ""
    if not destination_city:
        # Город не определён — считаем, что это город подачи.
        return pickup_city

    same = normalize_city(pickup_city) == normalize_city(destination_city)
    if same:
        return destination_city

    # Геокодер вернул другой город. Принимаем его, только если пассажир
    # действительно написал этот город в адресе.
    text = (destination_text or "").lower()
    if text and normalize_city(destination_city).lower() in text:
        return destination_city

    return pickup_city



def is_intercity(pickup_city: Optional[str], destination_city: Optional[str]) -> bool:
    """Является ли поездка межгородней (разные города подачи и назначения)."""
    return _is_intercity(pickup_city, destination_city)


def calculate_trip_price(distance: float, duration: int, *,
                         pickup_city: Optional[str] = None,
                         destination_city: Optional[str] = None) -> int:
    """Рассчитать стоимость поездки (округлённую)."""
    intercity = _is_intercity(pickup_city, destination_city)
    tariff = get_tariff_settings(city=pickup_city, intercity=intercity)
    base_price = _to_number(tariff.get("base_price", 100), 100)
    included_km = _to_number(tariff.get("included_km", 2), 2)
    price_per_km = _to_number(tariff.get("price_per_km", 20), 20)
    price_per_minute = _to_number(tariff.get("price_per_minute", 2), 2)
    min_price = _to_number(tariff.get("min_price", 50), 50)

    if distance <= included_km:
        trip_price = base_price
    else:
        trip_price = base_price + (distance - included_km) * price_per_km
    if price_per_minute > 0:
        trip_price += float(duration) * price_per_minute

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
                             destination_city: Optional[str] = None) -> Dict[str, Any]:
    """Подробный расчёт для показа пользователю."""
    intercity = _is_intercity(pickup_city, destination_city)
    tariff = get_tariff_settings(city=pickup_city, intercity=intercity)
    base_price = _to_number(tariff.get("base_price", 100), 100)
    included_km = _to_number(tariff.get("included_km", 2), 2)
    price_per_km = _to_number(tariff.get("price_per_km", 20), 20)
    price_per_minute = _to_number(tariff.get("price_per_minute", 2), 2)

    trip_price = base_price
    if distance > included_km:
        trip_price += (distance - included_km) * price_per_km
    if price_per_minute > 0:
        trip_price += float(duration) * price_per_minute

    waiting_cost = calculate_waiting_price(waiting_minutes, city=pickup_city, intercity=intercity)
    total = int(apply_rounding(trip_price + waiting_cost, tariff))

    return {
        "base_price": base_price,
        "included_km": included_km,
        "extra_km": max(0, distance - included_km),
        "extra_km_cost": max(0, (distance - included_km) * price_per_km),
        "duration_cost": duration * price_per_minute if price_per_minute > 0 else 0,
        "trip_price": int(trip_price),
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

    return (
        f"💰 <b>{title}</b>\n\n"
        f"🚕 Базовая стоимость: {tariff.get('base_price', 100)} ₽\n"
        f"📏 Включено км: {tariff.get('included_km', 2)} км\n"
        f"💵 Доплата за км: {tariff.get('price_per_km', 20)} ₽/км\n"
        f"⏰ Стоимость за минуту: {tariff.get('price_per_minute', 2)} ₽/мин\n"
        f"💳 Минимальная стоимость: {tariff.get('min_price', 50)} ₽\n"
        f"🔢 Округление: {tariff.get('rounding_type', 'up')} до {tariff.get('rounding_step', 10)} ₽\n"
        f"⏳ Бесплатное ожидание: {tariff.get('free_waiting_minutes', 3)} мин\n"
        f"💰 Плата за ожидание: {tariff.get('waiting_price_per_minute', 5)} ₽/мин"
    )

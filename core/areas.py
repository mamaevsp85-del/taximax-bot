"""Районы городов и населённые пункты зоны обслуживания.

Нужно для водительского списка заказов: заказы группируются
по районам («Крепость», «Девятый Трест», «Полярка», «Автовокзал»…),
а междугородние — по городам и посёлкам Хакасии и юга Красноярского края.

Район определяется по тексту адреса (микрорайон, улица, ориентир).
Список можно расширять — правки в этом файле.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.service_area import normalize_city

# ==================== Районы городов ====================
# город -> [(название района, [ключевые слова в адресе])]
# Ключевые слова сравниваются в нижнем регистре по вхождению в адрес.
CITY_DISTRICTS: Dict[str, List[Tuple[str, List[str]]]] = {
    "черногорск": [
        ("Крепость", ["крепост"]),
        ("Девятый Трест", ["9-й трест", "9 трест", "девятый трест", "9-го треста"]),
        ("Восьмой Трест", ["8-й трест", "8 трест", "восьмой трест"]),
        ("МЖК", ["мжк"]),
        ("Южный", ["южн"]),
        ("Заводская", ["заводск"]),
        ("Пригорск", ["пригорск"]),
        ("Центр", ["центр", "советская", "ленина", "космонавтов"]),
    ],
    "абакан": [
        ("Полярка", ["полярк", "полярный"]),
        ("Автовокзал", ["автовокзал", "катанова", "щетинкина"]),
        ("Северный", ["северн"]),
        ("Юго-Западный", ["юго-западн", "юз ", "юго западн"]),
        ("Центр", ["центр", "ленина", "пушкина", "хакасская"]),
        ("Заречье", ["заречь", "заречн"]),
        ("Верхняя Согра", ["верхняя согра", "согра"]),
        ("Нижняя Согра", ["нижняя согра"]),
        ("Красный Абакан", ["красный абакан", "красного абакана"]),
        ("МПС", ["мпс"]),
        ("Пятый микрорайон", ["5-й микрорайон", "5 микрорайон"]),
        ("Подсинино", ["подсинин"]),
    ],
    "саяногорск": [
        ("Центр", ["центр", "ленина"]),
        ("Черёмушки", ["черем", "черём"]),
        ("Майна", ["майна"]),
        ("Южный", ["южн"]),
    ],
    "минусинск": [
        ("Центр", ["центр", "ленина", "маркса"]),
        ("Южный", ["южн"]),
        ("Селиваниха", ["селиваних"]),
        ("Зелёный Бор", ["зелён", "зелен"]),
    ],
    "сорск": [("Центр", ["центр", "ленина"])],
    "абаза": [("Центр", ["центр", "ленина"])],
    "усть-абакан": [("Центр", ["центр", "ленина"])],
    "аскиз": [("Центр", ["центр", "ленина"])],
}

# ==================== Зона обслуживания ====================
# Хакасия: города и посёлки.
KHAKASSIA_SETTLEMENTS: List[str] = [
    "Абакан", "Черногорск", "Саяногорск", "Сорск", "Абаза",
    "Усть-Абакан", "Аскиз", "Белый Яр", "Боград", "Шира", "Копьёво",
    "Таштып", "Майна", "Черёмушки", "Пригорск", "Вершина Тёи",
    "Усть-Бюр", "Калинино", "Зелёное", "Солнечное", "Кирово",
    "Бея", "Табат", "Арбаты", "Малые Арбаты", "Полтаков",
    "Нижняя Тёя", "Верхняя Тёя", "Имек", "Бельтырский", "Ташеба",
    "Большая Ерба", "Малый Сютик", "Приисковый", "Троицкое",
    "Знаменка", "Бородино", "Бондарево", "Ефремкино", "Бискамжа",
    "Черный Июс", "Сонский", "Коммунар", "Топанов", "Усть-Сос",
    "Красный Катамор", "Алтайский", "Изыхские Копи", "Расцвет",
    "Очуры", "Новотроицкое", "Капчалы", "Чарков", "Весеннее",
]

# Юг Красноярского края.
SOUTH_KRAI_SETTLEMENTS: List[str] = [
    "Минусинск", "Шушенское", "Ермаковское", "Каратузское",
    "Идринское", "Краснотуранск", "Курагино", "Артёмовск",
    "Кошурниково", "Минусинский", "Селиваниха", "Зелёный Бор",
    "Казанцево", "Луговое", "Опытное Поле", "Тигрицкое",
    "Верхнеусинское", "Нижнеусинское", "Субботино", "Ильичево",
    "Каптырево", "Синеборск", "Выезжий Лог", "Кордово", "Красноярка",
    "Уджей", "Черемшанка", "Сизая", "Дубенское", "Саянск",
    "Большая Иня", "Малая Иня", "Восточное", "Кочергино",
    "Бельское", "Моторское", "Кирово", "Маринино", "Брагино",
]

ALL_SETTLEMENTS: List[str] = KHAKASSIA_SETTLEMENTS + SOUTH_KRAI_SETTLEMENTS

_NORMALIZED_SETTLEMENTS = {normalize_city(s) for s in ALL_SETTLEMENTS}

# Города, где есть внутригородские районы (для группировки «по районам»).
DISTRICT_CITIES = set(CITY_DISTRICTS.keys())


def normalize_place(name: str) -> str:
    return normalize_city(name or "")


def is_known_settlement(city: str) -> bool:
    return normalize_place(city) in _NORMALIZED_SETTLEMENTS


def detect_district(address: str, city: str = "") -> Optional[str]:
    """Район города по тексту адреса. None — район не определён."""
    text = (address or "").lower()
    if not text:
        return None
    key = normalize_place(city)
    variants: List[Tuple[str, List[str]]] = list(CITY_DISTRICTS.get(key, []))
    if not variants:
        # Город не указан — ищем по всем известным районам,
        # но только по «уникальным» ключевым словам.
        for rows in CITY_DISTRICTS.values():
            variants.extend(rows)
    best = None
    for name, words in variants:
        for w in words:
            pos = text.find(w)
            if pos >= 0:
                # Чем раньше в адресе встретилось слово — тем точнее.
                if best is None or pos < best[0]:
                    best = (pos, name)
                break
    return best[1] if best else None


# ==================== Группировка заказов ====================
def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def _coords(point: Any) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(point, dict):
        return None, None
    lat = point.get("lat", point.get("latitude"))
    lon = point.get("lon", point.get("longitude"))
    try:
        la = float(lat)
        lo = float(lon)
    except (TypeError, ValueError):
        return None, None
    if la == 0 and lo == 0:
        return None, None
    return la, lo


def order_distance_km(order: Dict[str, Any],
                      ref_lat: Optional[float],
                      ref_lon: Optional[float]) -> Optional[float]:
    """Расстояние от опорной точки водителя до точки подачи заказа."""
    if ref_lat is None or ref_lon is None or not order:
        return None
    lat, lon = _coords(order.get("pickup_coords"))
    if lat is None or lon is None:
        return None
    return _haversine_km(float(ref_lat), float(ref_lon), lat, lon)


def order_area_key(order: Dict[str, Any]) -> Tuple[str, str]:
    """(ключ, заголовок) группы для заказа.

    Межгород -> город назначения; иначе район города подачи;
    если район не определён -> «Другие адреса <город>».
    """
    pickup_city = (order.get("pickup_city") or "").strip()
    dest_city = (order.get("destination_city") or "").strip()
    if pickup_city and dest_city and normalize_place(pickup_city) != normalize_place(dest_city):
        return ("intercity:" + normalize_place(dest_city),
                f"Межгород · {dest_city}")
    district = detect_district(order.get("pickup") or "", pickup_city)
    if district:
        return (f"district:{normalize_place(pickup_city)}:{district.lower()}",
                f"{district} · {pickup_city}" if pickup_city else district)
    if pickup_city:
        return (f"city:{normalize_place(pickup_city)}", pickup_city)
    return ("other", "Другие адреса")


def group_orders_by_area(orders: Iterable[Dict[str, Any]],
                         ref_lat: Optional[float] = None,
                         ref_lon: Optional[float] = None) -> List[Dict[str, Any]]:
    """Группы районов/городов, внутри — от ближнего заказа к дальнему."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for order in orders or []:
        if not order:
            continue
        key, title = order_area_key(order)
        bucket = buckets.setdefault(key, {"key": key, "title": title,
                                         "count": 0, "orders": []})
        bucket["orders"].append(order)
        bucket["count"] += 1

    groups: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        ranked = []
        for order in bucket["orders"]:
            dist = order_distance_km(order, ref_lat, ref_lon)
            ranked.append((dist if dist is not None else 1e9, order))
        ranked.sort(key=lambda item: item[0])
        bucket["orders"] = [o for _, o in ranked]
        dists = [d for d, _ in ranked if d < 1e9]
        bucket["distance_km"] = round(min(dists), 2) if dists else None
        bucket["kind"] = "intercity" if bucket["key"].startswith("intercity:") else "district"
        groups.append(bucket)

    groups.sort(key=lambda g: (g["distance_km"] if g["distance_km"] is not None else 1e9,
                               -g["count"]))
    return groups
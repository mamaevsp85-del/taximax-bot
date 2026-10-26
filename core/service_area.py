"""Сервисная зона: белый список городов."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Set

from config import settings


def normalize_city(name: str) -> str:
    s = (name or "").strip().lower()
    prefixes = ("г. ", "г.", "пгт ", "пгт.", "рп ", "рп.", "с. ", "с.", "пос. ",
                "пос.", "посёлок ", "поселок ", "п. ", "п.", "город ",
                "рабочий посёлок ", "дачный посёлок ", "станица ", "ст-ца ")
    changed = True
    while changed:
        changed = False
        for pref in prefixes:
            if s.startswith(pref):
                s = s[len(pref):].strip()
                changed = True
                break
    while "  " in s:
        s = s.replace("  ", " ")
    return s


@lru_cache(maxsize=1)
def load_allowed_set() -> Set[str]:
    allowed: Set[str] = set()
    path = settings.service_area_file
    if not path or not os.path.exists(path):
        return allowed  # пустой набор = все города разрешены
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line:
                    allowed.add(normalize_city(line))
    except Exception:
        pass
    return allowed


def is_city_allowed(city_name: str) -> bool:
    # Ограничений по городам не было и не будет — разрешаем все города
    return True


def get_allowed_preview(limit: int = 15) -> List[str]:
    """Список разрешённых городов (ограничение по длине)."""
    # Ограничений по городам нет — возвращаем пустой список
    return []

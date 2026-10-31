"""Слияние баз MaximusTaxi и ТаксиMax в единый data/taxi_data.json.

Правила:
  • основой берётся ТаксиMax (свежее);
  • недостающие заказы/пользователи из MaximusTaxi добавляются;
  • при совпадении ключа побеждает запись с более поздней датой изменения;
  • order_counter = max(оба счётчика).
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "data" / "taxi_data.json"
# Приоритет: локальная устаревшая копия MaximusTax (внутри репозитория),
# затем внешняя папка Desktop\MaximusTaxi (ранее использовалась).
_LOCAL_LEGACY = ROOT / "MaximusTax" / "data" / "taxi_data.json"
_EXTERNAL_LEGACY = ROOT.parent / "MaximusTaxi" / "data" / "taxi_data.json"
OTHER = _LOCAL_LEGACY if _LOCAL_LEGACY.exists() else _EXTERNAL_LEGACY


def _stamp(record: dict) -> str:
    for key in ("completed_at", "cancelled_at", "created_at", "accepted_at", "rated_at"):
        val = record.get(key)
        if val:
            return str(val)
    return ""


def _merge_dict(base: dict, extra: dict, label: str) -> tuple[dict, int]:
    merged = dict(base)
    added = 0
    for key, value in extra.items():
        if key not in merged:
            merged[key] = value
            added += 1
            continue
        if isinstance(value, dict) and isinstance(merged[key], dict):
            if _stamp(value) > _stamp(merged[key]):
                merged[key] = value
    if added:
        print(f"  {label}: добавлено {added}")
    return merged, added


def main() -> int:
    if not MAIN.exists():
        print(f"Нет основного файла: {MAIN}")
        return 1
    if not OTHER.exists():
        print("Второй базы нет — слияние не требуется.")
        return 0

    base = json.loads(MAIN.read_text(encoding="utf-8"))
    extra = json.loads(OTHER.read_text(encoding="utf-8"))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(MAIN, MAIN.with_suffix(f".premerge_{stamp}.json"))

    users, users_added = _merge_dict(base.get("users", {}), extra.get("users", {}), "users")
    orders, orders_added = _merge_dict(base.get("orders", {}), extra.get("orders", {}), "orders")

    admin = dict(base.get("admin_data", {}))
    for key, value in (extra.get("admin_data") or {}).items():
        admin.setdefault(key, value)

    relay = dict(base.get("relay_sessions", {}))
    for key, value in (extra.get("relay_sessions") or {}).items():
        relay.setdefault(key, value)

    counter = max(int(base.get("order_counter", 0) or 0),
                  int(extra.get("order_counter", 0) or 0),
                  max((int(k) for k in orders), default=0))

    result = {
        "users": users,
        "orders": orders,
        "order_counter": counter,
        "admin_data": admin,
        "relay_sessions": relay,
    }
    MAIN.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Готово: users={len(users)} (+{users_added}), "
          f"orders={len(orders)} (+{orders_added}), order_counter={counter}")
    print(f"Файл: {MAIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

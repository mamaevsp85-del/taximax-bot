"""Разовая починка базы: снять устаревший флаг passenger_removed,
удалить фантомные записи и нормализовать роли."""
import json
import os
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "data", "taxi_data.json")


def main() -> int:
    with open(PATH, encoding="utf-8") as fh:
        data = json.load(fh)

    backup = PATH.replace(".json", f".fix_{datetime.now():%Y%m%d_%H%M%S}.json")
    shutil.copy2(PATH, backup)
    print(f"Бэкап: {os.path.basename(backup)}")

    users = data.get("users") or {}

    # 1) Убираем устаревший флаг, который отбирал роль пассажира.
    fixed = []
    for uid, u in users.items():
        if u.get("passenger_removed"):
            u.pop("passenger_removed", None)
            fixed.append(uid)
    print(f"Снят passenger_removed у: {fixed or '—'}")

    # 2) Удаляем фантомные записи (id <= 0).
    ghosts = [uid for uid in users if int(uid) <= 0]
    for uid in ghosts:
        users.pop(uid)
    print(f"Удалены фантомные пользователи: {ghosts or '—'}")

    # 3) active_role должна существовать в ролях пользователя.
    changed = []
    for uid, u in users.items():
        roles = ["passenger"]
        if u.get("driver_verification_status") == "approved":
            roles.append("driver")
        active = u.get("active_role") or "passenger"
        if active not in roles:
            u["active_role"] = roles[-1]
            changed.append((uid, active, u["active_role"]))
    print(f"Исправлен active_role: {changed or '—'}")

    data["users"] = users
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("Готово")
    return 0


if __name__ == "__main__":
    sys.exit(main())

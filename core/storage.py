"""
Ядро данных независимого MAX-бота «ТаксиМакс».

Реализует ролевую модель:
  - Каждый пользователь всегда имеет роль 'passenger'.
  - Роль 'driver' выдаётся после одобрения админом (верификация).
  - Роль 'admin' — из настроек (config.admin_ids).
  - У пользователя есть «активная роль» — в каком режиме он сейчас работает.

Хранение в памяти + персистентность в JSON. Без зависимостей от aiogram/Telegram.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import tempfile
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


ROLE_PASSENGER = "passenger"
ROLE_DRIVER = "driver"
ROLE_ADMIN = "admin"

ACTIVE_ORDER_STATUSES = ("pending", "accepted", "driver_arrived", "in_progress")


class Storage:
    def __init__(self, file_path: Optional[str] = None) -> None:
        self.file_path = file_path or settings.storage_file
        self.users: Dict[int, Dict[str, Any]] = {}
        self.orders: Dict[int, Dict[str, Any]] = {}
        self.order_counter = 0
        self.admin_data: Dict[str, Any] = self._default_admin_data()
        self.relay_sessions: Dict[int, Dict[str, Any]] = {}
        self.driver_sessions: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load()

    # ==================== АДМИН-ДАННЫЕ / ПЕРСИСТЕНТНОСТЬ ====================
    def _default_admin_data(self) -> Dict[str, Any]:
        return {
            "passenger_welcome": "🚗 Добро пожаловать в такси!\n\nВыберите действие:",
            "driver_welcome": "🚗 Меню водителя.\n\nВыберите действие:",
            "support_contact": settings.support_contact,
            "tariff_intercity": {
                "base_price": 150, "included_km": 5, "price_per_km": 35,
                "price_per_minute": 0, "min_price": 300,
                "rounding_type": "up", "rounding_step": 10,
                "free_waiting_minutes": 5, "waiting_price_per_minute": 5,
            },
            "tariffs_by_city": {},
            "tariff_edit_scope": "intercity",
            "pending_autoprice": {"enabled": False, "minute_step": 5,
                                  "type": "percent", "value": 10, "cap": None},
            "behavior_policy": {
                "decline_streak_block_threshold": 3,
                "decline_streak_block_minutes": 30,
                "cancel_day_block_threshold": 3,
                "cancel_day_block_minutes": 60,
            },
            "driver_payments": [],
            "managers": [],
            "admins": [],
            "driver_commission": {
                "enabled": False,          # False = бесплатный период, водители работают без комиссии
                "mode": "percent",         # "percent" (процент с заказов) | "access" (доступ по часам)
                "percent": 10,             # % с каждого заказа в режиме percent
                "access_prices": {"2": 50, "4": 80, "6": 100, "8": 150, "12": 200},
            },
        }

    def _load(self) -> None:
        with self._lock:
            if not os.path.exists(self.file_path):
                return
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as exc:
                logger.error("Не удалось прочитать %s: %s", self.file_path, exc)
                return
            self.users = {int(k): v for k, v in raw.get("users", {}).items()}
            self.orders = {int(k): v for k, v in raw.get("orders", {}).items()}
            self.order_counter = int(raw.get("order_counter", 0))
            for k, v in raw.get("admin_data", {}).items():
                self.admin_data[k] = v
            self.relay_sessions = {int(k): v for k, v in raw.get("relay_sessions", {}).items()}
            self.driver_sessions = {int(k): v for k, v in raw.get("driver_sessions", {}).items()}

    def save(self) -> None:
        with self._lock:
            tmp = None
            try:
                directory = os.path.dirname(os.path.abspath(self.file_path))
                os.makedirs(directory, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                                 dir=directory, suffix=".tmp",
                                                 delete=False) as f:
                    tmp = f.name
                    json.dump({
                        "users": self.users, "orders": self.orders,
                        "order_counter": self.order_counter, "admin_data": self.admin_data,
                        "relay_sessions": self.relay_sessions,
                        "driver_sessions": self.driver_sessions,
                    }, f, ensure_ascii=False, default=str)
                for attempt in range(5):
                    try:
                        os.replace(tmp, self.file_path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            except Exception as exc:
                logger.error("Не удалось сохранить данные: %s", exc)
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)

    def flush(self) -> None:
        self.save()

    def clear_users_and_orders(self) -> dict:
        """Полная очистка пользователей и заказов.

        Удаляет всех пользователей и все заказы, сбрасывает счётчик заказов.
        Настройки админки (тарифы, политики, менеджеры, приветствия) сохраняются.

        Returns:
            Статистика до очистки: сколько пользователей/заказов было удалено.
        """
        with self._lock:
            removed = {
                "users": len(self.users),
                "orders": len(self.orders),
                "order_counter": self.order_counter,
            }
            self.users = {}
            self.orders = {}
            self.order_counter = 0
            self.save()
            return removed

    # ==================== ПОЛЬЗОВАТЕЛИ ====================
    def ensure_user(self, user_id: int, name: str = "", username: str = "") -> Dict[str, Any]:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                user = {
                    "user_id": user_id, "name": name or "", "username": username,
                    "type": ROLE_PASSENGER, "active_role": ROLE_PASSENGER,
                    "driver_verification_status": "", "status": "offline",
                    "balance": 0.0, "rating": 5.0,
                    "orders_completed": 0, "orders_cancelled": 0,
                    "decline_streak": 0, "blocked_until": None,
                    "access_expires": None, "transactions": [],
                    "favorites": [], "notifications_enabled": True,
                    "phone": None, "car_model": None, "car_number": None, "car_color": None,
                    "car_photo_token": None, "driver_reject_reason": None,
                    "driver_application_at": None,
                    "driver_name": None, "driver_phone": None,
                    "created_at": datetime.now().isoformat(),
                }
                self.users[user_id] = user
                self.save()
            else:
                changed = False
                if name and not user.get("name"):
                    user["name"] = name; changed = True
                if username and user.get("username") != username:
                    user["username"] = username; changed = True
                if changed:
                    self.save()
            return self.users[user_id]

    def get_user(self, user_id: int) -> Dict[str, Any]:
        return self.users.get(int(user_id)) or {}

    def update_user(self, user_id: int, **fields) -> None:
        with self._lock:
            user = self.users.setdefault(int(user_id), {"user_id": int(user_id),
                                     "type": ROLE_PASSENGER, "active_role": ROLE_PASSENGER})
            user.update(fields)
            self.save()

    # ==================== РОЛИ ====================
    def compute_roles(self, user_id: int) -> list:
        """Роли пользователя.

        Пассажир — базовая роль: она есть у всех и не может быть снята.
        Водитель появляется после одобрения анкеты, админ — из настроек.
        """
        u = self.users.get(int(user_id), {})
        roles = [ROLE_PASSENGER]
        if u.get("driver_verification_status") == "approved":
            roles.append(ROLE_DRIVER)
        if self.is_admin(user_id):
            roles.append(ROLE_ADMIN)
        return roles

    def has_role(self, user_id: int, role: str) -> bool:
        return role in self.compute_roles(user_id)

    def get_active_role(self, user_id: int) -> str:
        u = self.users.get(int(user_id), {})
        active = u.get("active_role", ROLE_PASSENGER)
        roles = self.compute_roles(user_id)
        if active not in roles:
            # Default to first available role
            if ROLE_ADMIN in roles:
                active = ROLE_ADMIN
            elif ROLE_DRIVER in roles:
                active = ROLE_DRIVER
            else:
                active = ROLE_PASSENGER
        return active

    def set_active_role(self, user_id: int, role: str) -> bool:
        if role not in self.compute_roles(user_id):
            return False
        self.update_user(user_id, active_role=role)
        return True

    # Личные данные раздельно по ролям: у водителя — своё имя/телефон
    # (driver_name/driver_phone), у пассажира — общие name/phone. Если
    # водительские не заполнены — фолбэк на общие (обратная совместимость).
    def get_driver_name(self, user_id: int) -> str:
        u = self.get_user(user_id) or {}
        return u.get("driver_name") or u.get("name") or ""

    def get_driver_phone(self, user_id: int) -> str:
        u = self.get_user(user_id) or {}
        return u.get("driver_phone") or u.get("phone") or ""

    def get_passenger_name(self, user_id: int) -> str:
        u = self.get_user(user_id) or {}
        return u.get("name") or ""

    def get_passenger_phone(self, user_id: int) -> str:
        u = self.get_user(user_id) or {}
        return u.get("phone") or ""

    # ==================== МОДЕРАЦИЯ ВОДИТЕЛЕЙ ====================
    def get_verification_status(self, user_id: int) -> str:
        return self.users.get(int(user_id), {}).get("driver_verification_status", "")

    def set_verification_status(self, user_id: int, status: str) -> None:
        self.ensure_user(user_id)
        self.update_user(user_id, driver_verification_status=status)

    # Алиасы (используются в хендлерах) — единый публичный API
    def get_driver_verification_status(self, user_id: int) -> str:
        return self.get_verification_status(user_id)

    def request_driver_verification(self, user_id: int) -> None:
        """Пассажир отправляет заявку на роль водителя -> статус pending."""
        if self.get_verification_status(user_id) != "approved":
            self.set_verification_status(user_id, "pending")

    def approve_driver(self, user_id: int) -> None:
        self.update_user(user_id, driver_verification_status="approved", type=ROLE_DRIVER,
                         driver_reject_reason=None)

    def reject_driver(self, user_id: int, reason: str = "") -> None:
        """Отклонить заявку с обязательной причиной (её увидит водитель)."""
        self.set_verification_status(user_id, "rejected")
        self.update_user(user_id, driver_reject_reason=(reason or "").strip() or None)

    def submit_driver_application(self, user_id: int, *, car_number: str = "",
                                  car_color: str = "", car_photo_token: Optional[str] = None,
                                  car_model: str = "") -> None:
        """Сохранить данные анкеты и отправить заявку на модерацию (status=pending).

        Заявка без номера авто или фото на модерацию не уходит.
        """
        self.ensure_user(user_id)
        self.update_user(
            user_id,
            car_number=(car_number or "").strip() or None,
            car_color=(car_color or "").strip() or None,
            car_photo_token=car_photo_token or None,
            driver_verification_status="pending",
            driver_reject_reason=None,
            driver_application_at=datetime.now().isoformat(),
        )

    def get_driver_application(self, user_id: int) -> Dict[str, Any]:
        """Анкета водителя: что заполнено и что ещё нужно прислать."""
        u = self.users.get(int(user_id), {})
        return {
            "car_number": u.get("car_number"),
            "car_color": u.get("car_color"),
            "car_photo_token": u.get("car_photo_token"),
            "status": u.get("driver_verification_status", ""),
        }

    def get_reject_reason(self, user_id: int) -> str:
        return self.users.get(int(user_id), {}).get("driver_reject_reason") or ""

    def get_approved_drivers(self) -> list:
        return self.get_drivers()

    def demote_driver(self, user_id: int) -> bool:
        """Забрать права водителя: сбросить верификацию, данные авто, доступ.

        Пользователь остаётся в системе как пассажир и может подать анкету снова.
        """
        uid = int(user_id)
        u = self.users.get(uid)
        if not u:
            return False
        if u.get("driver_verification_status") != "approved":
            return False
        self.update_user(
            uid,
            driver_verification_status="",
            driver_reject_reason=None,
            driver_application_at=None,
            car_number=None,
            car_color=None,
            car_photo_token=None,
            car_model=None,
            access_expires=None,
            type=ROLE_PASSENGER,
        )
        return True

    # ==================== АДМИНИСТРАТОРЫ ====================
    def get_admin_ids(self) -> List[int]:
        """Список ID админов: из .env (settings.admin_ids) + из админ-данных (admins)."""
        ids = list(settings.admin_ids)
        for entry in self.admin_data.get("admins", []):
            mid = entry.get("admin_id")
            if mid is not None and int(mid) not in ids:
                ids.append(int(mid))
        return ids

    def is_admin(self, user_id: int) -> bool:
        return int(user_id) in self.get_admin_ids()

    def find_user_by_phone(self, phone: str) -> Optional[int]:
        """Найти user_id по номеру телефона (нормализованный: только цифры)."""
        digits = "".join(ch for ch in str(phone) if ch.isdigit())
        if not digits:
            return None
        for uid, u in self.users.items():
            uph = u.get("phone") or ""
            ud = "".join(ch for ch in str(uph) if ch.isdigit())
            if ud and ud == digits:
                return uid
        return None

    def add_admin_by_phone(self, phone: str) -> tuple:
        """Добавить админа по номеру телефона.

        Возвращает (user_id, ok). Пользователь должен быть известен системе
        (т.е. иметь сохранённый номер телефона).
        """
        uid = self.find_user_by_phone(phone)
        if uid is None:
            return None, False
        if uid in settings.admin_ids:
            return uid, False
        admins = self.admin_data.setdefault("admins", [])
        if any(a.get("admin_id") == uid for a in admins):
            return uid, False
        user = self.users.get(uid) or {}
        admins.append({"admin_id": uid, "name": user.get("name", ""),
                       "phone": user.get("phone")})
        self.save()
        return uid, True

    def add_admin(self, user_id: int) -> bool:
        """Добавить админа по user_id (только если его нет ни в .env, ни в admins)."""
        uid = int(user_id)
        if uid in settings.admin_ids:
            return False
        admins = self.admin_data.setdefault("admins", [])
        if any(a.get("admin_id") == uid for a in admins):
            return False
        user = self.users.get(uid) or {}
        admins.append({"admin_id": uid, "name": user.get("name", "")})
        self.save()
        return True

    def remove_admin(self, user_id: int) -> bool:
        """Удалить админа из списка (только если он не из .env)."""
        uid = int(user_id)
        if uid in settings.admin_ids:
            return False
        admins = self.admin_data.get("admins", [])
        before = len(admins)
        self.admin_data["admins"] = [a for a in admins if a.get("admin_id") != uid]
        changed = len(self.admin_data["admins"]) != before
        if changed:
            self.save()
        return changed


    def get_pending_drivers(self) -> list:
        return [
            {**u, "user_id": uid}
            for uid, u in self.users.items()
            if u.get("driver_verification_status") == "pending"
        ]

    def get_drivers(self) -> list:
        return [
            {**u, "user_id": uid}
            for uid, u in self.users.items()
            if u.get("driver_verification_status") == "approved"
        ]

    # ==================== ЗАКАЗЫ ====================
    def create_order(self, data: dict) -> int:
        with self._lock:
            self.order_counter += 1
            oid = self.order_counter
            p = self.users.get(int(data.get("passenger_id", 0)), {})
            pickup_city = data.get("pickup_city", "")
            dest_city = data.get("destination_city", "")
            order = {
                "id": oid,
                "passenger_id": data.get("passenger_id"),
                "driver_id": data.get("driver_id"),
                "city": pickup_city,
                "pickup": data.get("pickup", ""),
                "destination": data.get("destination", ""),
                "pickup_coords": data.get("pickup_coords"),
                "destination_coords": data.get("destination_coords"),
                "price": data.get("price", 0),
                "distance": data.get("distance", 0),
                "time_minutes": data.get("time_minutes", 0),
                "status": data.get("status", "pending"),
                "created_at": datetime.now().isoformat(),
                "accepted_at": None,
                "completed_at": None,
                "waiting_started_at": None,
                "waiting_cost": 0.0,
                "pickup_city": pickup_city,
                "destination_city": dest_city,
                "is_intercity": data.get("is_intercity", False),
                "passenger_snapshot": {"name": p.get("name"), "phone": p.get("phone")},
                "driver_snapshot": None,
                "notified_driver_ids": [],
                "viewed_driver_ids": [],
                "declined_driver_ids": [],
            }
            self.orders[oid] = order
            self.save()
            return oid

    def get_order(self, order_id: int) -> dict:
        return self.orders.get(int(order_id), {})

    # ==================== РЕЛЕЙ-СЕССИИ (персистентные) ====================
    def set_relay_session(self, user_id: int, partner_id: int, order_id: int,
                          hours: float = 24.0) -> None:
        until = (datetime.now() + timedelta(hours=hours)).isoformat()
        sess = {
            "user_id": int(user_id), "partner_id": int(partner_id),
            "order_id": int(order_id), "until": until,
        }
        with self._lock:
            self.relay_sessions[int(user_id)] = {
                **sess, "partner_id": int(partner_id),
            }
            self.relay_sessions[int(partner_id)] = {
                **sess, "user_id": int(partner_id), "partner_id": int(user_id),
            }
            self.save()

    def get_active_relay_for_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        sess = self.relay_sessions.get(int(user_id))
        if not sess:
            return None
        until = sess.get("until")
        if until:
            try:
                until_dt = datetime.fromisoformat(until) if isinstance(until, str) else until
                if datetime.now() > until_dt:
                    self.relay_sessions.pop(int(user_id), None)
                    self.save()
                    return None
            except Exception:
                pass
        return sess

    def stop_relay_session(self, user_id: int) -> None:
        with self._lock:
            self.relay_sessions.pop(int(user_id), None)
            self.save()

    # ==================== ИЗБРАННОЕ (пассажиры) ====================
    def add_favorite(self, user_id: int, order_id: int, label: str = "") -> None:
        with self._lock:
            u = self.users.setdefault(int(user_id), {"user_id": int(user_id)})
            favs = u.setdefault("favorites", [])
            entry = {"order_id": int(order_id), "label": label or "",
                     "added_at": datetime.now().isoformat()}
            if not any(f.get("order_id") == int(order_id) for f in favs):
                favs.append(entry)
                self.save()

    def remove_favorite(self, user_id: int, order_id: int) -> bool:
        with self._lock:
            u = self.users.get(int(user_id))
            if not u:
                return False
            favs = u.get("favorites", [])
            before = len(favs)
            u["favorites"] = [f for f in favs if f.get("order_id") != int(order_id)]
            if len(u["favorites"]) != before:
                self.save()
                return True
            return False

    def get_favorites(self, user_id: int) -> List[Dict[str, Any]]:
        u = self.users.get(int(user_id), {})
        return list(u.get("favorites", []) or [])

    # ==================== РЕЙТИНГ ПОЕЗДКИ ====================
    def set_order_rating(self, order_id: int, rating: int,
                         passenger_id: Optional[int] = None) -> bool:
        with self._lock:
            o = self.orders.get(int(order_id))
            if not o:
                return False
            if passenger_id is not None and int(o.get("passenger_id", 0)) != int(passenger_id):
                return False
            o["passenger_rating"] = int(rating)
            o["rated_at"] = datetime.now().isoformat()
            self.save()
            return True

    def get_order_rating(self, order_id: int) -> Optional[int]:
        o = self.orders.get(int(order_id), {})
        val = o.get("passenger_rating")
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    # ==================== АВТО-ОТМЕНА ТАЙМАУТ-ЗАКАЗОВ ====================
    def cancel_timeout_orders(self, timeout_minutes: int = 5) -> List[int]:
        """Отменяет pending-заказы старше timeout_minutes.

        Возвращает список order_id, которые были отменены.
        """
        cancelled: List[int] = []
        now = datetime.now()
        with self._lock:
            for oid, o in list(self.orders.items()):
                if o.get("status") != "pending":
                    continue
                ca = o.get("created_at")
                if not ca:
                    continue
                try:
                    dt = datetime.fromisoformat(ca) if isinstance(ca, str) else ca
                except Exception:
                    continue
                if (now - dt).total_seconds() >= timeout_minutes * 60:
                    o["status"] = "cancelled"
                    o["cancelled_by"] = "timeout"
                    o["cancelled_at"] = now.isoformat()
                    cancelled.append(int(oid))
            if cancelled:
                self.save()
        return cancelled

    def update_order(self, order_id: int, **fields) -> None:
        with self._lock:
            o = self.orders.get(int(order_id))
            if o:
                o.update(fields)
                self.save()

    def get_orders_by_passenger(self, passenger_id: int) -> list:
        return [o for o in self.orders.values() if o.get("passenger_id") == int(passenger_id)]

    def get_orders_by_driver(self, driver_id: int) -> list:
        return [o for o in self.orders.values() if o.get("driver_id") == int(driver_id)]

    def get_pending_orders(self) -> list:
        return [o for o in self.orders.values() if o.get("status") == "pending"]

    def get_passenger_active_order(self, passenger_id: int) -> dict:
        active_statuses = ("pending", "accepted", "driver_arrived", "in_progress")
        for o in sorted(self.orders.values(), key=lambda x: x.get("id", 0), reverse=True):
            if o.get("passenger_id") == int(passenger_id) and o.get("status") in active_statuses:
                return o
        return {}

    def get_driver_active_order(self, driver_id: int) -> dict:
        active_statuses = ("accepted", "driver_arrived", "in_progress")
        for o in sorted(self.orders.values(), key=lambda x: x.get("id", 0), reverse=True):
            if o.get("driver_id") == int(driver_id) and o.get("status") in active_statuses:
                return o
        return {}


    def add_notified_driver(self, order_id: int, driver_id: int) -> None:
        with self._lock:
            o = self.orders.get(int(order_id))
            if o and driver_id not in o.setdefault("notified_driver_ids", []):
                o["notified_driver_ids"].append(driver_id)
                self.save()

    def mark_driver_declined(self, order_id: int, driver_id: int) -> None:
        with self._lock:
            o = self.orders.get(int(order_id))
            if o and driver_id not in o.setdefault("declined_driver_ids", []):
                o["declined_driver_ids"].append(driver_id)
                self.save()


    # ==================== ОНЛАЙН ВОДИТЕЛИ И ДОСТУП ====================
    def is_driver_blocked(self, driver_id: int) -> bool:
        u = self.users.get(int(driver_id), {})
        until = u.get("blocked_until")
        if until:
            try:
                until_dt = datetime.fromisoformat(until)
                if until_dt > datetime.now():
                    return True
                self.update_user(driver_id, blocked_until=None, decline_streak=0)
            except Exception:
                return False
        return False

    def get_online_drivers(self) -> list:
        """Онлайн-водители (approved + статус online).

        Проверка платного доступа снята: на старте водители работают без
        комиссии и без платного доступа, поэтому «Выйти на линию» сразу
        делает водителя видимым в списке онлайн-водителей.
        """
        result = []
        for uid, u in self.users.items():
            if u.get("driver_verification_status") != "approved":
                continue
            if u.get("status") != "online":
                continue
            if self.is_driver_blocked(uid):
                continue
            result.append({**u, "user_id": uid})
        return result

    def has_active_access(self, driver_id: int) -> bool:
        """Проверяет, есть ли у водителя право выйти на линию.

        Логика:
        • комиссия отключена (enabled=False) — полный доступ, без ограничений;
        • комиссия включена, режим «percent» — доступ не нужен, платит % с заказов;
        • комиссия включена, режим «access» — нужен активный access_expires.
        """
        cfg = self.get_commission_config()
        if not cfg.get("enabled"):
            return True
        if cfg.get("mode") != "access":
            return True
        u = self.users.get(int(driver_id), {})
        exp = u.get("access_expires")
        if not exp:
            return False
        try:
            exp_dt = datetime.fromisoformat(exp) if isinstance(exp, str) else exp
            return exp_dt > datetime.now()
        except Exception:
            return False

    def grant_access(self, driver_id: int, hours: int) -> None:
        """Выдать доступ водителю на N часов (админская выдача)."""
        u = self.users.get(int(driver_id), {})
        exp = u.get("access_expires")
        try:
            exp_dt = datetime.fromisoformat(exp) if isinstance(exp, str) else exp
        except Exception:
            exp_dt = None
        base = exp_dt if (exp_dt and exp_dt > datetime.now()) else datetime.now()
        new_exp = base + timedelta(hours=hours)
        self.update_user(driver_id, access_expires=new_exp.isoformat())

    def check_access(self, driver_id: int) -> dict:
        u = self.users.get(int(driver_id), {})
        return {
            "has_access": self.has_active_access(driver_id),
            "expires": u.get("access_expires"),
            "balance": u.get("balance", 0),
        }

    # ==================== КОМИССИЯ ВОДИТЕЛЕЙ ====================
    def get_commission_config(self) -> Dict[str, Any]:
        """Конфиг комиссии (всегда с полными значениями по умолчанию)."""
        cfg = self.admin_data.get("driver_commission")
        if not isinstance(cfg, dict):
            cfg = {}
        cfg.setdefault("enabled", False)
        cfg.setdefault("mode", "percent")
        cfg.setdefault("percent", 10)
        cfg.setdefault("access_prices",
                       {"2": 50, "4": 80, "6": 100, "8": 150, "12": 200})
        self.admin_data["driver_commission"] = cfg
        return cfg

    def set_commission(self, **fields) -> None:
        """Обновить поля конфига комиссии и сохранить.

        Если процент установлен на 0 — комиссия отключается полностью:
        скрываются тарифы доступа (покупка смен становится невозможной),
        меню «плат» не отображается у водителей.
        """
        with self._lock:
            cfg = self.get_commission_config()
            if "percent" in fields:
                try:
                    percent = _to_float(fields["percent"])
                except (TypeError, ValueError):
                    percent = 0.0
                if percent <= 0:
                    # Отключаем: не продаём платные смены, балансы/доступ не нужны.
                    fields.setdefault("enabled", False)
                    fields.setdefault("mode", "percent")
                    fields["access_prices"] = {}
            cfg.update(fields)
            self.admin_data["driver_commission"] = cfg
            self.save()

    def commission_requires_access(self) -> bool:
        """Нужен ли оплаченный доступ для выхода на линию.

        False — когда комиссия выключена (бесплатный период)
        или выбран режим «процент с заказов».
        """
        cfg = self.get_commission_config()
        if not cfg.get("enabled"):
            return False
        return cfg.get("mode") == "access"

    def get_access_tariffs(self) -> Dict[int, float]:
        """Включённые тарифы доступа {часы: цена}, отсортированы по часам.

        При отключённой комиссии (enabled=False) тарифы не показываются —
        возвращается пустой словарь, покупка смен невозможна.
        """
        cfg = self.get_commission_config()
        if not cfg.get("enabled"):
            return {}
        prices = cfg.get("access_prices") or {}
        result: Dict[int, float] = {}
        for key, val in prices.items():
            try:
                result[int(key)] = _to_float(val, 0.0)
            except (TypeError, ValueError):
                continue
        if not result:
            result = {2: 50, 4: 80, 6: 100, 8: 150, 12: 200}
        return dict(sorted(result.items()))

    def set_access_price(self, hours: int, price: float) -> None:
        """Задать цену за доступ на N часов."""
        with self._lock:
            cfg = self.get_commission_config()
            cfg.setdefault("access_prices", {})[str(int(hours))] = _to_float(price, 0.0)
            self.save()

    def remove_access_price(self, hours: int) -> None:
        """Убрать тариф доступа на N часов."""
        with self._lock:
            cfg = self.get_commission_config()
            ap = cfg.setdefault("access_prices", {})
            ap.pop(str(int(hours)), None)
            ap.pop(int(hours), None)  # на случай, если ключ остался int в памяти
            self.save()

    def balances_disabled(self) -> bool:
        """True — у водителей скрыты все напоминания и упоминания о балансе."""
        return bool(self.admin_data.get("balances_disabled"))

    def set_balances_disabled(self, enabled: bool) -> None:
        """Включить/выключить режим скрытия балансов у водителей."""
        with self._lock:
            if enabled:
                self.admin_data["balances_disabled"] = True
            else:
                self.admin_data.pop("balances_disabled", None)
            self.save()

    def apply_order_commission(self, driver_id: int, fare: float) -> float:
        """Списывает комиссию с выполненного заказа в режиме «процент».

        Начисляет на баланс водителю сумму за вычетом комиссии
        (баланс = чистый заработок). Комиссия пишется в статистику и леджер.

        Returns:
            Сумма комиссии (0.0 — если комиссия не применяется).
        """
        with self._lock:
            cfg = self.get_commission_config()
            if not cfg.get("enabled") or cfg.get("mode") != "percent":
                return 0.0
            pct = _to_float(cfg.get("percent", 0), 0.0)
            if pct <= 0 or fare <= 0:
                return 0.0
            commission = round(fare * pct / 100.0, 2)
            u = self.users.get(int(driver_id))
            if u is not None:
                u["total_commission"] = round(
                    _to_float(u.get("total_commission", 0), 0.0) + commission, 2)
                u["last_commission"] = commission
            self.admin_data.setdefault("driver_payments", []).append({
                "driver_id": int(driver_id),
                "type": "commission",
                "amount": commission,
                "at": datetime.now().isoformat(),
            })
            self.save()
            return commission

    # ==================== ГЕОПОЗИЦИЯ ВОДИТЕЛЯ ====================
    def update_driver_location(self, driver_id: int, lat: float, lon: float) -> None:
        self.update_user(driver_id, last_lat=lat, last_lon=lon,
                         last_location_at=datetime.now().isoformat())

    # ==================== СТАТИСТИКА ====================
    def bump_driver_stat(self, driver_id: int, field: str, delta: int = 1) -> None:
        with self._lock:
            u = self.users.get(int(driver_id))
            if u is None:
                return
            u[field] = u.get(field, 0) + delta
            self.save()

    # ==================== АЛИАСЫ (публичный API, используемый в хендлерах) ====================
    def get_or_create_user(self, user_id: int, name: str = "", username: str = "") -> Dict[str, Any]:
        """Алиас ensure_user (вызывается из роутера при /start)."""
        return self.ensure_user(user_id, name=name, username=username)

    def set_driver_verification_status(self, user_id: int, status: str) -> None:
        """Алиас set_verification_status (вызывается из common.py)."""
        self.set_verification_status(user_id, status)

    def has_driver_access(self, driver_id: int) -> bool:
        """Алиас has_active_access."""
        return self.has_active_access(driver_id)

    def get_access_expires(self, driver_id: int) -> Optional[datetime]:
        """Доступ до (datetime) или None."""
        u = self.users.get(int(driver_id), {})
        exp = u.get("access_expires")
        if not exp:
            return None
        try:
            return datetime.fromisoformat(exp) if isinstance(exp, str) else exp
        except Exception:
            return None

    def get_balance(self, user_id: int) -> float:
        u = self.users.get(int(user_id), {})
        try:
            return float(u.get("balance", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def deduct_balance(self, user_id: int, amount: float) -> bool:
        """Списать средства. False — если недостаточно."""
        with self._lock:
            current = self.get_balance(user_id)
            if current < float(amount):
                return False
            self.update_user(user_id, balance=round(current - float(amount), 2))
            return True

    def add_balance(self, user_id: int, amount: float) -> None:
        with self._lock:
            new_val = round(self.get_balance(user_id) + float(amount), 2)
            self.update_user(user_id, balance=new_val)

    def add_access_hours(self, driver_id: int, hours: int) -> None:
        """Алиас grant_access (покупка доступа водителем)."""
        self.grant_access(driver_id, hours)

    def set_online_status(self, user_id: int, status: str) -> None:
        """Установить статус водителя: online/offline/break."""
        self.update_user(user_id, status=status)

    def get_driver_stats(self, driver_id: int, period: str = "today") -> dict:
        """Статистика водителя за период: {'count': N, 'earnings': X}.

        period:
          "today"           — календарные сутки (с 00:00 сегодня);
          "week"/"month"    — за последние N дней;
          "date:YYYY-MM-DD" — за конкретный календарный день (любой день).
        """
        deltas = {"week": 7, "month": 30}
        today = datetime.now().date()
        if period == "today":
            since = datetime.combine(today, datetime.min.time())
            until = datetime.combine(today + timedelta(days=1), datetime.min.time())
        elif isinstance(period, str) and period.startswith("date:"):
            try:
                d = datetime.strptime(period[5:], "%Y-%m-%d").date()
            except Exception:
                d = today
            since = datetime.combine(d, datetime.min.time())
            until = datetime.combine(d + timedelta(days=1), datetime.min.time())
        elif period in deltas:
            since = datetime.now() - timedelta(days=deltas[period])
            until = datetime.now()
        else:
            since = datetime.now() - timedelta(days=1)
            until = datetime.now()
        count = 0
        earnings = 0.0
        for o in self.orders.values():
            if o.get("driver_id") != int(driver_id):
                continue
            if o.get("status") != "completed":
                continue
            ts = o.get("completed_at") or o.get("accepted_at") or o.get("created_at")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
            except Exception:
                continue
            if since <= dt < until:
                count += 1
                try:
                    earnings += float(o.get("price", 0) or 0)
                    earnings += float(o.get("waiting_cost", 0) or 0)
                except (TypeError, ValueError):
                    pass
        return {"count": count, "earnings": earnings}

    # ==================== АВТО-ПОВЫШЕНИЕ ЦЕНЫ ОЖИДАНИЯ (pending-заказы) ====================
    def check_order_timeout(self) -> List[int]:
        """Возвращает ID pending-заказов, ожидающих водителя >= 5 минут."""
        ids: List[int] = []
        for oid, o in self.orders.items():
            if o.get("status") != "pending":
                continue
            ca = o.get("created_at")
            if not ca:
                continue
            try:
                dt = datetime.fromisoformat(ca) if isinstance(ca, str) else ca
            except Exception:
                continue
            if (datetime.now() - dt).total_seconds() >= 5 * 60:
                ids.append(oid)
        return ids

    def apply_pending_order_auto_increase(self, order_id: int) -> Optional[int]:
        """Автоповышение цены ожидания для pending-заказа.
        Читает политику admin_data['pending_autoprice'] (minute_step, type, value, cap).
        Возвращает новую цену (int), если изменилась; иначе None.
        """
        try:
            order = self.orders.get(int(order_id))
            if not order or order.get("status") != "pending":
                return None
            ca = order.get("created_at")
            if not ca:
                return None
            try:
                ca = datetime.fromisoformat(ca) if isinstance(ca, str) else ca
            except Exception:
                return None
            minutes = int((datetime.now() - ca).total_seconds() // 60)
            cfg = self.admin_data.get("pending_autoprice", {})
            if not bool(cfg.get("enabled", False)):
                return None
            step_min = int(cfg.get("minute_step", 5) or 5)
            if minutes < step_min or minutes % step_min != 0:
                return None
            last_inc = int(order.get("pending_price_last_increase_minute", 0) or 0)
            if last_inc >= minutes:
                return None
            base_price = float(
                order.get("price") or order.get("final_price") or order.get("calculated_price") or 0)
            inc_type = (cfg.get("type") or "percent").lower()
            inc_value = float(cfg.get("value") or (10.0 if inc_type == "percent" else 20.0))
            cap_any = cfg.get("cap")
            if inc_type == "fixed":
                new_price = base_price + inc_value
            else:
                new_price = base_price * (1.0 + inc_value / 100.0)
            try:
                from core.tariff_manager import get_tariff_settings, apply_rounding
                city = (order.get("city") or "").strip()
                tariff = get_tariff_settings(city=city)
                new_price = apply_rounding(new_price, tariff)
            except Exception:
                pass
            if cap_any is not None:
                try:
                    if new_price > float(cap_any):
                        new_price = float(cap_any)
                except Exception:
                    pass
            # Если после округления не выросло — просто отметим минуту
            if int(new_price) <= int(base_price):
                order["pending_price_last_increase_minute"] = minutes
                self.save()
                return None
            self.update_order(int(order_id), price=new_price)
            order = self.orders.get(int(order_id)) or {}
            order["pending_price_last_increase_minute"] = minutes
            hist = order.get("auto_price_history") or []
            hist.append({
                "minute": minutes, "from": float(base_price), "to": float(new_price),
                "type": inc_type, "value": float(inc_value), "ts": datetime.now().isoformat(),
            })
            order["auto_price_history"] = hist
            self.save()
            return int(new_price)
        except Exception as exc:
            logger.error("Автоповышение цены заказа #%s: %s", order_id, exc)
            return None
    # ==================== ОТКАЗЫ / БЛОКИРОВКИ ====================
    def record_driver_decline(self, driver_id: int) -> bool:
        """Зафиксировать отказ водителя от заказа. Увеличивает серию отказов."""
        policy = self.admin_data.get("behavior_policy", {})
        threshold = int(policy.get("decline_streak_block_threshold", 3))
        block_min = int(policy.get("decline_streak_block_minutes", 30))
        with self._lock:
            u = self.users.get(int(driver_id))
            if u is None:
                return False
            u["decline_streak"] = int(u.get("decline_streak", 0) or 0) + 1
            newly = u["decline_streak"] >= threshold
            if newly:
                u["blocked_until"] = (datetime.now() + timedelta(minutes=block_min)).isoformat()
                u["decline_streak"] = 0
            self.save()
            return newly

    def record_driver_cancel(self, driver_id: int) -> bool:
        """Зафиксировать отмену заказа водителем. Блокирует при превышении порога."""
        policy = self.admin_data.get("behavior_policy", {})
        threshold = int(policy.get("cancel_day_block_threshold", 3))
        block_min = int(policy.get("cancel_day_block_minutes", 60))
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            u = self.users.get(int(driver_id))
            if u is None:
                return False
            if u.get("_cancel_day") != today:
                u["_cancel_day"] = today
                u["_cancel_count"] = 0
            u["_cancel_count"] = int(u.get("_cancel_count", 0) or 0) + 1
            newly = u["_cancel_count"] >= threshold
            if newly:
                u["blocked_until"] = (datetime.now() + timedelta(minutes=block_min)).isoformat()
                u["_cancel_count"] = 0
            self.save()
            return newly

    def reset_driver_decline_streak(self, driver_id: int) -> None:
        with self._lock:
            u = self.users.get(int(driver_id))
            if u is not None:
                u["decline_streak"] = 0
                self.save()

    def get_driver_block_info(self, driver_id: int) -> Dict[str, Any]:
        u = self.users.get(int(driver_id), {})
        return {
            "blocked": self.is_driver_blocked(driver_id),
            "block_until": u.get("blocked_until"),
            "decline_streak": int(u.get("decline_streak", 0) or 0),
        }

    def add_transaction(self, user_id: int, type: str, amount: float = 0.0,
                        hours: int = 0, description: str = "") -> None:
        """Добавить запись о транзакции."""
        with self._lock:
            u = self.users.setdefault(int(user_id), {
                "user_id": int(user_id), "type": ROLE_PASSENGER,
                "active_role": ROLE_PASSENGER, "driver_verification_status": "",
                "status": "offline", "balance": 0.0,
            })
            txns = u.setdefault("transactions", [])
            txns.append({
                "type": type, "amount": round(float(amount), 2),
                "hours": int(hours or 0), "description": description,
                "created_at": datetime.now().isoformat(),
            })
            self.save()

    def get_transaction_history(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        u = self.users.get(int(user_id), {})
        return list(reversed(u.get("transactions", [])))[:limit]

    def is_driver_profile_complete(self, user_id: int) -> bool:
        u = self.users.get(int(user_id), {})
        if not u:
            return False
        required = [u.get("name") or u.get("first_name"), u.get("username"),
                    u.get("car_model"), u.get("car_number"), u.get("car_color")]
        return all(bool(x) for x in required)

    def has_driver_docs(self, user_id: int) -> bool:
        u = self.users.get(int(user_id), {})
        keys = ("selfie_photo_token", "car_photo_token", "car_photo_left_id",
                "car_photo_right_id", "car_photo_front_id", "car_photo_back_id",
                "car_photo_interior_id")
        return all(u.get(k) for k in keys)

    def get_driver_history(self, driver_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        """Завершённые заказы водителя (история), новые первыми."""
        result = []
        for o in sorted(self.orders.values(), key=lambda x: x.get("id", 0), reverse=True):
            if o.get("driver_id") != int(driver_id):
                continue
            if o.get("status") != "completed":
                continue
            result.append(o)
            if len(result) >= limit:
                break
        return result

    def get_driver_period_stats(self, driver_id: int, period: str = "today") -> Dict[str, Any]:
        """Расширенная статистика водителя за период: day/week/month/all."""
        now = datetime.now()
        if period == "day":
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
            until = now
        elif period == "week":
            since = now - timedelta(days=7)
            until = now
        elif period == "month":
            since = now - timedelta(days=30)
            until = now
        elif period == "all":
            since = None
            until = now
        else:
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
            until = now
        orders_total = 0
        orders_completed = 0
        revenue = 0.0
        for o in self.orders.values():
            if o.get("driver_id") != int(driver_id):
                continue
            ts = o.get("completed_at") or o.get("accepted_at") or o.get("created_at")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
            except Exception:
                continue
            if since and dt < since:
                continue
            if dt >= until:
                continue
            orders_total += 1
            if o.get("status") == "completed":
                orders_completed += 1
            try:
                revenue += float(o.get("price", 0) or 0)
                revenue += float(o.get("waiting_cost", 0) or 0)
            except (TypeError, ValueError):
                pass
        u = self.users.get(int(driver_id), {})
        return {
            "orders_total": orders_total, "orders_completed": orders_completed,
            "revenue": round(revenue, 2), "rating": u.get("rating", 5.0),
            "count": u.get("orders_completed", 0), "earnings": float(u.get("earnings", 0.0) or 0.0),
        }

    def get_driver_order_queue(self, driver_id: int) -> List[int]:
        return list(self.users.get(int(driver_id), {}).get("driver_order_queue", []))

    def add_to_driver_queue(self, driver_id: int, order_id: int) -> bool:
        with self._lock:
            u = self.users.get(int(driver_id))
            if u is None:
                return False
            q = u.setdefault("driver_order_queue", [])
            if order_id not in q:
                q.append(order_id)
                self.save()
                return True
            return False

    def remove_from_driver_queue(self, driver_id: int, order_id: int) -> None:
        with self._lock:
            u = self.users.get(int(driver_id), {})
            q = u.get("driver_order_queue")
            if q and order_id in q:
                q.remove(order_id)
                self.save()

    # ==================== АДМИН-API: ВОДИТЕЛИ ====================
    def get_all_drivers(self) -> list:
        """Все водители (есть авто/роль/статус) с полями для админки."""
        result = []
        for uid, u in self.users.items():
            if int(uid) <= 0:
                continue  # служебные/битые записи в админку не попадают
            is_driver = (u.get("type") == ROLE_DRIVER or u.get("car_model")
                         or u.get("driver_verification_status"))
            if not is_driver:
                continue
            result.append({
                "user_id": uid,
                "name": u.get("driver_name") or u.get("name") or "Без имени",
                "username": u.get("username") or "",
                "phone": u.get("driver_phone") or u.get("phone") or "",
                "car_model": u.get("car_model") or "",
                "car_color": u.get("car_color") or "",
                "car_number": u.get("car_number") or "",
                "status": u.get("status", "offline"),
                "verification_status": u.get("driver_verification_status", ""),
                "is_blocked": self.is_driver_blocked(uid),
                "block_until": u.get("blocked_until"),
                "priority_seconds": int(u.get("priority_seconds", 0) or 0),
                "current_city": u.get("current_city") or "",
                "orders_count": len(self.get_orders_by_driver(uid)),
                "earnings": u.get("earnings", 0) or 0,
                "balance": self.get_balance(uid),
                "has_access": self.has_active_access(uid),
            })
        return result

    def get_driver_by_id(self, driver_id: int) -> dict:
        for d in self.get_all_drivers():
            if d["user_id"] == int(driver_id):
                return d
        return {}

    def block_driver(self, driver_id: int, duration_minutes=None) -> bool:
        uid = int(driver_id)
        if uid not in self.users:
            return False
        until = None
        if duration_minutes:
            until = (datetime.now() + timedelta(minutes=int(duration_minutes))).isoformat()
        self.update_user(uid, blocked_until=until, decline_streak=0)
        return True

    def unblock_driver(self, driver_id: int) -> bool:
        uid = int(driver_id)
        if uid not in self.users:
            return False
        self.update_user(uid, blocked_until=None)
        return True

    def set_driver_priority(self, driver_id: int, priority_seconds: int) -> bool:
        uid = int(driver_id)
        if uid not in self.users:
            return False
        self.update_user(uid, priority_seconds=max(0, int(priority_seconds)))
        return True

    def remove_driver_priority(self, driver_id: int) -> bool:
        return self.set_driver_priority(driver_id, 0)

    def get_driver_priority(self, driver_id: int) -> int:
        return int(self.users.get(int(driver_id), {}).get("priority_seconds", 0) or 0)

    def grant_unlimited_access(self, driver_id: int) -> None:
        far = (datetime.now() + timedelta(days=365 * 100)).isoformat()
        self.update_user(int(driver_id), access_expires=far)

    def reset_access(self, driver_id: int) -> None:
        self.update_user(int(driver_id), access_expires=None)

    def delete_user(self, user_id: int) -> bool:
        uid = int(user_id)
        with self._lock:
            if uid not in self.users:
                return False
            del self.users[uid]
            self.save()
            return True

    def search_drivers_by_car(self, query: str) -> list:
        q = (query or "").strip().lower()
        if not q:
            return []
        return [d for d in self.get_all_drivers()
                if q in (d.get("car_number") or "").lower()]


    # ==================== АДМИН-API: ПАССАЖИРЫ ====================
    def get_all_passengers(self) -> list:
        result = []
        for uid, u in self.users.items():
            if int(uid) <= 0:
                continue  # служебные/битые записи в админку не попадают
            if u.get("driver_verification_status") == "approved":
                continue  # показываем в списке водителей
            orders = self.get_orders_by_passenger(uid)
            completed = [o for o in orders if o.get("status") == "completed"]
            spent = sum(float(o.get("price", 0) or 0) + float(o.get("waiting_cost", 0) or 0)
                        for o in completed)
            result.append({
                "user_id": uid,
                "name": u.get("name") or "Без имени",
                "username": u.get("username") or "",
                "phone": u.get("phone") or "",
                "is_blocked": self.is_driver_blocked(uid),
                "block_until": u.get("blocked_until"),
                "orders_count": len(orders),
                "completed_orders": len(completed),
                "total_spent": round(spent, 2),
                "last_active": u.get("last_active") or "",
            })
        return result

    def block_passenger(self, passenger_id: int, duration_minutes=None) -> bool:
        uid = int(passenger_id)
        if uid not in self.users:
            return False
        until = None
        if duration_minutes:
            until = (datetime.now() + timedelta(minutes=int(duration_minutes))).isoformat()
        self.update_user(uid, blocked_until=until)
        return True

    def unblock_passenger(self, passenger_id: int) -> bool:
        return self.unblock_driver(passenger_id)

    # ==================== АДМИН-API: МЕНЕДЖЕРЫ ====================
    def get_managers(self) -> list:
        return list(self.admin_data.get("managers", []))

    def add_manager(self, user_id: int) -> None:
        uid = int(user_id)
        mgrs = self.admin_data.setdefault("managers", [])
        if uid not in mgrs:
            mgrs.append(uid)
            self.save()

    def remove_manager(self, user_id: int) -> None:
        uid = int(user_id)
        mgrs = self.admin_data.setdefault("managers", [])
        if uid in mgrs:
            mgrs.remove(uid)
            self.save()

    def is_manager(self, user_id: int) -> bool:
        return int(user_id) in self.admin_data.get("managers", [])

    # ==================== АДМИН-API: ЗАКАЗЫ ====================
    def get_orders(self, status=None, limit=50) -> list:
        orders = sorted(self.orders.values(), key=lambda o: o.get("id", 0), reverse=True)
        if status:
            orders = [o for o in orders if o.get("status") == status]
        return orders[:limit]

    def assign_driver_to_order(self, order_id: int, driver_id: int) -> bool:
        oid = int(order_id)
        o = self.orders.get(oid)
        if not o or o.get("status") != "pending":
            return False
        d = self.get_user(driver_id)
        self.update_order(oid, driver_id=int(driver_id), status="accepted",
                          accepted_at=datetime.now().isoformat(),
                          driver_snapshot={"name": d.get("driver_name") or d.get("name") or "Не указан",
                                           "phone": d.get("driver_phone") or d.get("phone") or "",
                                           "car_model": d.get("car_model") or "",
                                           "car_number": d.get("car_number") or ""})
        return True

    def cancel_order(self, order_id: int) -> bool:
        oid = int(order_id)
        if oid not in self.orders:
            return False
        self.update_order(oid, status="cancelled")
        return True

    def update_order_status(self, order_id: int, new_status: str) -> bool:
        oid = int(order_id)
        if oid not in self.orders:
            return False
        fields = {"status": new_status}
        if new_status == "completed":
            fields["completed_at"] = datetime.now().isoformat()
        self.update_order(oid, **fields)
        return True

    def update_order_price(self, order_id: int, new_price: float) -> bool:
        oid = int(order_id)
        if oid not in self.orders:
            return False
        self.update_order(oid, price=float(new_price))
        return True

    def get_driver_session(self, driver_id: int) -> Dict[str, Any]:
        """Get driver session for state management."""
        with self._lock:
            return self.driver_sessions.get(int(driver_id), {})

    def set_driver_session(self, driver_id: int, state: str = "IDLE",
                           **kwargs: Any) -> None:
        """Set or update driver session."""
        with self._lock:
            session = self.driver_sessions.setdefault(int(driver_id), {
                "driver_id": int(driver_id),
                "state": state,
                "active_order_id": None,
                "list_message_id": None,
                "reference_lat": None,
                "reference_lon": None,
                "last_list_update": None,
                **kwargs,
            })
            session.update(kwargs)
            session["last_updated"] = datetime.now().isoformat()
            self.save()
        oid = int(order_id)
        if oid not in self.orders:
            return False
        self.update_order(oid, city=(new_city or "").strip())
        return True

    def get_orders_by_city(self, city: str, status=None, limit=200) -> list:
        from core.service_area import normalize_city
        key = normalize_city(city or "")
        res = []
        for o in sorted(self.orders.values(), key=lambda x: x.get("id", 0), reverse=True):
            oc = normalize_city(o.get("city") or o.get("pickup_city") or "")
            if key and oc != key:
                continue
            if status and o.get("status") != status:
                continue
            res.append(o)
            if len(res) >= limit:
                break
        return res

    def get_orders_without_city(self, status=None, limit=200) -> list:
        res = []
        for o in sorted(self.orders.values(), key=lambda x: x.get("id", 0), reverse=True):
            if (o.get("city") or o.get("pickup_city") or "").strip():
                continue
            if status and o.get("status") != status:
                continue
            res.append(o)
            if len(res) >= limit:
                break
        return res

    def has_orders_without_city(self) -> bool:
        return len(self.get_orders_without_city(limit=1)) > 0

    def get_cities_list(self) -> list:
        cities = set()
        for o in self.orders.values():
            c = (o.get("city") or o.get("pickup_city") or "").strip()
            if c:
                cities.add(c)
        return sorted(cities)

    def get_city_stats(self) -> dict:
        stats = {}
        for o in self.orders.values():
            c = (o.get("city") or o.get("pickup_city") or "").strip()
            if not c:
                continue
            s = stats.setdefault(c, {"total": 0, "pending": 0, "accepted": 0,
                                     "completed": 0, "cancelled": 0})
            s["total"] += 1
            st = o.get("status")
            if st in s:
                s[st] += 1
        return stats


    # ==================== АДМИН-API: ПОЛИТИКА ПОВЕДЕНИЯ ====================
    def get_behavior_policy(self) -> dict:
        p = dict(self.admin_data.get("behavior_policy", {}))
        p.setdefault("decline_block_enabled", True)
        p.setdefault("decline_streak_block_threshold", 3)
        p.setdefault("decline_streak_block_minutes", 30)
        p.setdefault("cancel_block_enabled", True)
        p.setdefault("cancel_streak_enabled", True)
        p.setdefault("cancel_streak_block_threshold", 2)
        p.setdefault("cancel_streak_block_minutes", 60)
        p.setdefault("cancel_day_block_threshold", 3)
        p.setdefault("cancel_day_block_minutes", 60)
        self.admin_data["behavior_policy"] = p
        return p

    def update_behavior_policy(self, changes: dict) -> dict:
        with self._lock:
            p = self.get_behavior_policy()
            p.update(changes)
            self.admin_data["behavior_policy"] = p
            self.save()
            return p

    def get_driver_cancel_stats(self, driver_id: int, window_hours: int = 12) -> dict:
        orders = self.get_orders_by_driver(driver_id)
        cancelled = [o for o in orders if o.get("status") == "cancelled"]
        return {"streak": int(self.users.get(int(driver_id), {}).get("decline_streak", 0) or 0),
                "cancelled": len(cancelled), "window_hours": window_hours}

    # ==================== АДМИН-API: КОНТЕНТ ====================
    _CONTENT_DEFAULTS = {
        "passenger_content": "🚕 Добро пожаловать! Здесь вы можете заказать такси.",
        "driver_content": "🚗 Меню водителя. Выйдите на линию, чтобы получать заказы.",
        "passenger_contact": "",
        "driver_contact": "",
        "passenger_welcome": "🚗 Добро пожаловать в такси!\n\nВыберите действие:",
        "driver_welcome": "🚗 Меню водителя.\n\nВыберите действие:",
        "passenger_instructions": "Выберите адрес подачи и назначения, затем подтвердите заказ.",
        "driver_instructions": "Выйдите на линию и принимайте заказы вашего города.",
        "error_messages": "❌ Произошла ошибка. Попробуйте ещё раз.",
        "success_messages": "✅ Готово!",
        "passenger_buttons": "",
        "driver_buttons": "",
        "contact_buttons": "",
        "external_links": "",
    }

    def get_content(self, key: str) -> str:
        if key in self.admin_data:
            return self.admin_data.get(key) or ""
        return self._CONTENT_DEFAULTS.get(key, "")

    def set_content(self, key: str, value: str) -> None:
        with self._lock:
            self.admin_data[key] = value
            self.save()


    # ==================== АДМИН-API: СТАТИСТИКА ====================
    @staticmethod
    def _parse_dt(value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return datetime.now()
        return value or datetime.now()

    def get_detailed_stats(self) -> dict:
        now = datetime.now()
        today = now.date()
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)
        orders = list(self.orders.values())
        completed = [o for o in orders if o.get("status") == "completed"]
        today_orders = [o for o in orders if self._parse_dt(o.get("created_at")).date() == today]
        week_orders = [o for o in orders if self._parse_dt(o.get("created_at")) >= week_ago]
        month_orders = [o for o in orders if self._parse_dt(o.get("created_at")) >= month_ago]
        drivers = self.get_all_drivers()

        def rev(lst):
            return round(sum(float(o.get("price", 0) or 0) + float(o.get("waiting_cost", 0) or 0)
                             for o in lst if o.get("status") == "completed"), 2)

        total_rev = rev(orders)
        earnings = {}
        for d in drivers:
            do = [o for o in completed if o.get("driver_id") == d["user_id"]]
            earnings[d["name"]] = round(sum(
                float(o.get("price", 0) or 0) + float(o.get("waiting_cost", 0) or 0) for o in do), 2)
        top_drivers = sorted(earnings.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "passengers_total": len(self.get_all_passengers()),
            "drivers_total": len(drivers),
            "drivers_online": len(self.get_online_drivers()),
            "orders_total": len(orders),
            "orders_today": len(today_orders),
            "orders_week": len(week_orders),
            "orders_month": len(month_orders),
            "orders_pending": len([o for o in orders if o.get("status") == "pending"]),
            "orders_accepted": len([o for o in orders if o.get("status") == "accepted"]),
            "orders_completed": len(completed),
            "orders_cancelled": len([o for o in orders if o.get("status") == "cancelled"]),
            "revenue_total": total_rev,
            "revenue_today": rev(today_orders),
            "revenue_week": rev(week_orders),
            "revenue_month": rev(month_orders),
            "avg_order_price": round(total_rev / len(completed), 2) if completed else 0,
            "avg_daily_orders": len(today_orders),
            "avg_weekly_orders": round(len(week_orders) / 7, 1),
            "top_drivers": top_drivers,
            "last_updated": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def get_revenue_stats(self, period="day", date_from=None, date_to=None) -> dict:
        now = datetime.now()
        if period == "day":
            date_from = datetime.combine(now.date(), datetime.min.time())
            date_to = now
        elif period == "week":
            date_from = now - timedelta(days=7); date_to = now
        elif period == "month":
            date_from = now - timedelta(days=30); date_to = now
        else:
            date_from = date_from or (now - timedelta(days=3650))
            date_to = date_to or now
        scope = [o for o in self.orders.values()
                 if date_from <= self._parse_dt(o.get("created_at")) <= date_to]
        completed = [o for o in scope if o.get("status") == "completed"]
        revenue = round(sum(float(o.get("price", 0) or 0) + float(o.get("waiting_cost", 0) or 0)
                            for o in completed), 2)
        return {
            "orders_total": len(scope),
            "orders_pending": len([o for o in scope if o.get("status") == "pending"]),
            "orders_accepted": len([o for o in scope if o.get("status") == "accepted"]),
            "orders_completed": len(completed),
            "orders_cancelled": len([o for o in scope if o.get("status") == "cancelled"]),
            "revenue": revenue,
        }

    def get_realtime_stats(self) -> dict:
        now = datetime.now()
        recent = now - timedelta(minutes=5)
        online_users = len([u for u in self.users.values()
                            if self._parse_dt(u.get("last_active")) >= recent])
        orders = list(self.orders.values())
        return {
            "online_users": online_users,
            "active_orders": len([o for o in orders if o.get("status") in
                                  ("pending", "accepted", "driver_arrived", "in_progress")]),
            "pending_orders": len([o for o in orders if o.get("status") == "pending"]),
            "accepted_orders": len([o for o in orders if o.get("status") == "accepted"]),
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        }


    def disable_commission_for_all(self) -> dict:
        """Полное отключение комиссии и всех платных функций для всех водителей.

        При отключении:
          • commissions выключена (enabled=False) — бесплатный период;
          • балансы всех водителей обнуляются;
          • платный доступ (access_expires) снимается;
          • включается режим скрытия балансов (balances_disabled=True),
            чтобы у водителей пропали все упоминания баланса/смен.

        Returns: сколько водителей затронуто.
        """
        with self._lock:
            affected = 0
            for uid, u in self.users.items():
                if uid in settings.admin_ids:
                    continue
                changed = False
                if (u.get("balance") or 0) != 0:
                    u["balance"] = 0.0; changed = True
                if u.get("access_expires"):
                    u["access_expires"] = None; changed = True
                if changed:
                    affected += 1
            cfg = self.get_commission_config()
            cfg["enabled"] = False
            cfg["mode"] = "percent"
            self.admin_data["driver_commission"] = cfg
            self.admin_data["balances_disabled"] = True
            self.save()
            return {"drivers_affected": affected}

    def enable_commission(self) -> None:
        """Включить комиссию и разрешить показ балансов."""
        with self._lock:
            self.set_commission(enabled=True)
            self.set_balances_disabled(False)

    def reset_all_driver_balances(self) -> int:
        """Обнулить балансы всех водителей (без изменения режима комиссии)."""
        with self._lock:
            n = 0
            for uid, u in self.users.items():
                if uid in settings.admin_ids:
                    continue
                if (u.get("balance") or 0) != 0:
                    u["balance"] = 0.0; n += 1
            self.save()
            return n

    def get_commission_summary(self) -> dict:
        """Сводка по комиссии для админ-панели."""
        cfg = self.get_commission_config()
        total_commission = 0.0
        for u in self.users.values():
            total_commission += _to_float(u.get("total_commission", 0), 0.0)
        return {
            "enabled": bool(cfg.get("enabled")),
            "mode": cfg.get("mode", "percent"),
            "percent": _to_float(cfg.get("percent", 0), 0.0),
            "balances_disabled": self.balances_disabled(),
            "requires_access": self.commission_requires_access(),
            "access_prices": self.get_access_tariffs(),
            "total_commission": round(total_commission, 2),
        }

    # ==================== ЭКЗЕМПЛЯР ====================


def haversine_distance(lat1, lon1, lat2, lon2):
    """Расстояние между двумя точками, км."""
    import math
    earth_km = 6371.0
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return earth_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_available_orders_for_driver(driver_id, ref_lat=None, ref_lon=None):
    """Pending-заказы, ближайшие к точке водителя — первыми."""
    driver = storage.get_user(driver_id)
    if not driver:
        return []
    if ref_lat is None or ref_lon is None:
        ref_lat = driver.get("last_lat")
        ref_lon = driver.get("last_lon")
    pending = storage.get_pending_orders()
    if ref_lat is None or ref_lon is None:
        return pending
    ranked = []
    for order in pending:
        coords = order.get("pickup_coords") or {}
        lat = coords.get("lat")
        lon = coords.get("lon")
        if lat is None or lon is None:
            continue
        ranked.append((haversine_distance(ref_lat, ref_lon, lat, lon), order))
    ranked.sort(key=lambda item: item[0])
    return [order for _, order in ranked]


# Глобальный экземпляр хранилища
storage = Storage()


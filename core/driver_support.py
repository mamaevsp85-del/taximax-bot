"""
Поддержка роли «Водитель» в MAX-боте: постоянные состояния (очередь заказов,
таймеры ожидания, релей-сессии), построители меню и вспомогательные
функции. Логика сопоставлена с Telegram-ботомом водителя (driver_bot_clean.py).
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from core import storage
from core.fsm import get_context
from max_api.keyboards import callback_button
from ui.reply import get_menu_mid, HTML_FORMAT

logger = logging.getLogger(__name__)
S = storage.storage

# Бесплатное ожидание: FREE_WAIT_MINUTES минут, затем WAIT_PER_MINUTE ₽/мин.
FREE_WAIT_MINUTES = 3
WAIT_PER_MINUTE = 5.0
# Как часто тикер обновляет карточку водителя и проверяет переход в платное.
WAIT_TICK_SECONDS = 20

# ==================== Константы FSM-состояний ====================
STATE_REG_NAME = "driver:reg_name"
STATE_REG_CAR_MODEL = "driver:reg_car_model"
STATE_REG_CAR_NUMBER = "driver:reg_car_number"
STATE_REG_CAR_COLOR = "driver:reg_car_color"
STATE_AWAIT_LOCATION = "driver:await_location"
STATE_AWAIT_FINAL_GEO = "driver:await_final_geo"
STATE_UPLOAD_SELFIE = "driver:upload_selfie"
STATE_UPLOAD_CAR_LEFT = "driver:upload_car_left"
STATE_UPLOAD_CAR_RIGHT = "driver:upload_car_right"
STATE_UPLOAD_CAR_FRONT = "driver:upload_car_front"
STATE_UPLOAD_CAR_BACK = "driver:upload_car_back"
STATE_UPLOAD_CAR_INTERIOR = "driver:upload_car_interior"
STATE_EDIT_NAME = "driver:edit_name"
STATE_EDIT_PHONE = "driver:edit_phone"
STATE_EDIT_CAR_MODEL = "driver:edit_car_model"
STATE_EDIT_CAR_NUMBER = "driver:edit_car_number"
STATE_EDIT_CAR_COLOR = "driver:edit_car_color"
STATE_BALANCE_TOPUP = "driver:balance_topup"
STATE_STATS_CUSTOM = "driver:stats_custom"
STATE_RELAY = "driver:relay"

# ==================== Runtime state (transient) ====================
_driver_waiting_order_id: Dict[int, int] = {}
_waiting_tasks: Dict[int, asyncio.Task] = {}
# Контекст активного ожидания: order_id -> {client, driver_id, passenger_id, paid_notified}
_waiting_ctx: Dict[int, Dict[str, Any]] = {}
_last_menu_message: Dict[int, str] = {}
_driver_chat_messages: Dict[int, set] = {}


# ==================== Профиль водителя ====================


# ==================== Менеджер очереди заказов ====================
def add_order_to_driver_queue(driver_id: int, order_id: int) -> bool:
    return S.add_to_driver_queue(driver_id, order_id)


def get_driver_order_queue(driver_id: int) -> List[int]:
    return S.get_driver_order_queue(driver_id)


def get_driver_current_order(driver_id: int) -> Optional[int]:
    u = S.get_user(driver_id) or {}
    cur = u.get("driver_current_order_id")
    q = S.get_driver_order_queue(driver_id)
    for oid in ([cur] if cur else []) + q:
        order = S.get_order(int(oid))
        if (order and order.get("driver_id") == driver_id
                and order.get("status") in ("accepted", "driver_arrived", "in_progress")):
            return int(oid)
    return None


def is_driver_busy(driver_id: int) -> bool:
    cur = get_driver_current_order(driver_id)
    if not cur:
        return False
    order = S.get_order(cur)
    return bool(order and order.get("status") in ("accepted", "driver_arrived", "in_progress"))


def remove_order_from_driver_queue(driver_id: int, order_id: int) -> None:
    S.remove_from_driver_queue(driver_id, order_id)
    u = S.get_user(driver_id) or {}
    if u.get("driver_current_order_id") == order_id:
        S.update_user(driver_id, driver_current_order_id=None)
    q = S.get_driver_order_queue(driver_id)
    if q:
        S.update_user(driver_id, driver_current_order_id=q[0])


# ==================== Таймер ожидания ====================
def start_waiting(order_id: int, client=None, driver_id: Optional[int] = None,
                  passenger_id: Optional[int] = None) -> None:
    """Начать таймер ожидания для заказа.

    При передаче ``client``/``driver_id`` запускается живой тикер: каждые
    ``WAIT_TICK_SECONDS`` он редактирует карточку заказа у водителя, показывая
    текущее время ожидания и накопленную сумму. Как только бесплатное время
    заканчивается — пассажиру отправляется одно уведомление о платном ожидании.
    """
    order = S.get_order(order_id)
    if not order:
        return
    if not order.get("waiting_started_at"):
        S.update_order(order_id, waiting_started_at=datetime.now().isoformat(),
                       waiting_ended_at=None)
    if client is not None and driver_id is not None:
        _waiting_ctx[order_id] = {
            "client": client,
            "driver_id": driver_id,
            "passenger_id": passenger_id if passenger_id is not None
                            else order.get("passenger_id"),
            "paid_notified": False,
        }
        cancel_waiting_ticker(order_id)

        async def _ticker():
            try:
                while True:
                    await asyncio.sleep(WAIT_TICK_SECONDS)
                    order = S.get_order(order_id)
                    if not order or order.get("waiting_ended_at"):
                        break
                    info = get_waiting_info(order_id)
                    await _refresh_driver_waiting_card(order_id, info)
                    ctx = _waiting_ctx.get(order_id)
                    if ctx and not info["is_free"] and not ctx.get("paid_notified"):
                        ctx["paid_notified"] = True
                        await _notify_passenger_paid_waiting(order_id, info)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # защита от падения фоновой задачи
                logger.warning("Тикер ожидания #%s упал: %s", order_id, exc)

        _waiting_tasks[order_id] = asyncio.create_task(_ticker())


async def _refresh_driver_waiting_card(order_id: int, info: Dict[str, Any]) -> None:
    """Обновить карточку заказа у водителя живым таймером ожидания."""
    ctx = _waiting_ctx.get(order_id)
    if not ctx:
        return
    order = S.get_order(order_id)
    if not order:
        return
    mid = get_menu_mid(ctx["driver_id"]) or get_last_menu_message_id(ctx["driver_id"])
    if not mid:
        return
    try:
        from handlers.driver import _show_order_detail
        await _show_order_detail(ctx["client"], ctx["driver_id"], order_id, order,
                                 edit_id=mid)
    except Exception as exc:
        logger.debug("Не удалось обновить карточку ожидания #%s: %s", order_id, exc)


async def _notify_passenger_paid_waiting(order_id: int, info: Dict[str, Any]) -> None:
    """Сообщить пассажиру, что началось платное ожидание (однократно)."""
    ctx = _waiting_ctx.get(order_id)
    if not ctx:
        return
    pid = ctx.get("passenger_id")
    if not pid:
        return
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return
    try:
        await ctx["client"].send_message(
            user_id=pid_int,
            text=(f"⏳ <b>Началось платное ожидание</b> по заказу #{order_id}\n\n"
                  f"Прошло: {info['waiting_time_text']}\n"
                  f"Начислено за ожидание: {fmt_price(info['cost'])}\n"
                  f"Тариф: {fmt_price(WAIT_PER_MINUTE)}/мин после "
                  f"{FREE_WAIT_MINUTES} бесплатных мин."))
    except Exception as exc:
        logger.debug("Уведомление пассажиру о платном ожидании #%s: %s", order_id, exc)


def stop_waiting(order_id: int) -> None:
    order = S.get_order(order_id)
    if not order:
        return
    info = get_waiting_info(order_id)
    total = info["waiting_seconds_total"]
    now = datetime.now().isoformat()
    S.update_order(order_id, waiting_started_at=None, waiting_ended_at=now,
                   waiting_seconds_total=total, waiting_cost=info["cost"])
    cancel_waiting_ticker(order_id)


def get_waiting_info(order_id: int) -> Dict[str, Any]:
    order = S.get_order(order_id) or {}
    started = order.get("waiting_started_at")
    ended = order.get("waiting_ended_at")
    waiting_seconds = int(order.get("waiting_seconds_total", 0) or 0)
    if started and not ended:
        try:
            dt = datetime.fromisoformat(started)
            waiting_seconds += max(0, int((datetime.now(dt.tzinfo) - dt).total_seconds()))
        except Exception:
            logger.warning("Некорректное время ожидания заказа %s", order_id)
    free_minutes, per_minute = FREE_WAIT_MINUTES, WAIT_PER_MINUTE
    free_seconds = free_minutes * 60
    if waiting_seconds <= free_seconds:
        cost, is_free = 0.0, True
    else:
        cost = ((waiting_seconds - free_seconds) / 60.0) * per_minute
        is_free = False
    free_left = max(0, (free_seconds - waiting_seconds) / 60.0)
    m = max(0, math.floor(waiting_seconds / 60.0))
    return {
        "waiting_time": m, "waiting_time_text": f"{m}:{int(waiting_seconds % 60):02d}",
        "waiting_seconds_total": waiting_seconds, "is_free": is_free,
        "free_minutes_left": max(0, math.floor(free_left)), "cost": round(cost, 2),
    }


def calculate_waiting_cost(order_id: int) -> float:
    return get_waiting_info(order_id).get("cost", 0.0)


def cancel_waiting_ticker(order_id: int) -> None:
    t = _waiting_tasks.pop(order_id, None)
    if t and not t.done():
        t.cancel()
    _driver_waiting_order_id.pop(order_id, None)
    _waiting_ctx.pop(order_id, None)


# ==================== Релей-сессии ====================
def start_relay_session(user_id: int, partner_id: int, order_id: int, hours: float = 24.0) -> None:
    S.set_relay_session(user_id, partner_id, order_id, hours=hours)


def get_active_relay_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    return S.get_active_relay_for_user(user_id)


def get_relay_partner_id(user_id: int) -> Optional[int]:
    sess = get_active_relay_for_user(user_id)
    return sess.get("partner_id") if sess else None


def stop_relay_session(user_id: int) -> None:
    S.stop_relay_session(user_id)


# ==================== Отслеживание сообщений / меню ====================
def track_sent_message(user_id: int, message_id: str) -> None:
    _driver_chat_messages.setdefault(user_id, set()).add(message_id)


def clear_driver_chat_messages(user_id: int) -> List[str]:
    """Удалить все отслеживаемые сообщения бота в диалоге водителя."""
    ids = _driver_chat_messages.pop(user_id, set())
    return list(ids)


def get_last_menu_message_id(user_id: int) -> Optional[str]:
    u = S.get_user(user_id) or {}
    return u.get("last_menu_message_id")


def set_last_menu_message_id(user_id: int, message_id: str) -> None:
    S.update_user(user_id, last_menu_message_id=message_id)


# ==================== Меню-построители ====================
def _row(btn_text: str, payload: str) -> List[Dict[str, Any]]:
    return [callback_button(btn_text, payload)]


def _status_menu_label(user_id: int) -> str:
    u = S.get_user(user_id) or {}
    s = u.get("status", "offline")
    if s == "online":
        return "🟢 Выйти на линию (уже на линии)"
    if s == "break":
        return "⏸ Перерыв"
    return "🔴 Статус работы (оффлайн)"


def balance_hidden() -> bool:
    """True — у водителей скрыты все напоминания и упоминания о балансе."""
    return S.balances_disabled()


def main_menu_rows(user_id: int) -> List[List[Dict[str, Any]]]:
    return [
        _row(_status_menu_label(user_id), "driver:work_status"),
        _row("🚕 Текущие заказы", "driver:current_order"),
        _row("📋 Список заказов", "driver:orders_available"),
        _row("📍 Обновить гео", "driver:location"),
        _row("👤 Мой профиль", "driver:profile"),
        _row("🔄 Сменить роль", "menu:roles"),
    ]


def work_status_rows() -> List[List[Dict[str, Any]]]:
    return [
        _row("✅ Выйти на линию", "driver:online"),
        _row("❌ Уйти с линии", "driver:offline"),
        _row("⏸ Перерыв", "driver:break"),
        _row("↪️ Главное меню", "driver:menu"),
    ]


def access_rows() -> List[List[Dict[str, Any]]]:
    rows: List[List[Dict[str, Any]]] = []
    # Платные функции отключены админом (комиссия выключена) — смены не покупаются.
    if balance_hidden() or not S.get_access_tariffs():
        rows.append(_row("🔕 Платные смены отключены", "driver:profile"))
        rows.append(_row("↪️ Главное меню", "driver:menu"))
        return rows
    for hours, price in sorted(S.get_access_tariffs().items()):
        rows.append(_row(f"⏱ {hours} ч — {int(price)} ₽", f"driver:balance:buy:{hours}")
)
    rows.append(_row("↪️ Главное меню", "driver:menu"))
    return rows


def balance_rows(user_id: int) -> List[List[Dict[str, Any]]]:
    if balance_hidden():
        return back_to_main_rows()
    bal = S.get_balance(user_id)
    rows = [_row(f"📥 Пополнить баланс (баланс: {int(bal)} ₽)", "driver:balance:topup")]
    for hours, price in sorted(S.get_access_tariffs().items()):
        rows.append(_row(f"⏱ {hours} ч — {int(price)} ₽", f"driver:balance:buy:{hours}"
))
    rows.append(_row("↪️ Главное меню", "driver:menu"))
    return rows


def _driver_is_admin_or_manager(user_id: int) -> bool:
    return settings.is_admin(user_id) or S.is_manager(user_id)


def balance_rows_for_user(user_id: int) -> List[List[Dict[str, Any]]]:
    return balance_rows(user_id)



def stats_rows() -> List[List[Dict[str, Any]]]:
    return [
        _row("📅 Сегодня", "driver:stats:day"),
        _row("🗓 Неделя", "driver:stats:week"),
        _row("🗓 Месяц", "driver:stats:month"),
        _row("📈 Произвольный период", "driver:stats:custom"),
        _row("📊 Общая сводка", "driver:stats:general"),
        _row("↪️ Главное меню", "driver:menu"),
    ]


def profile_rows() -> List[List[Dict[str, Any]]]:
    rows = [
        _row("✏️ Редактировать имя", "driver:profile:edit_name"),
        _row("📱 Изменить телефон", "driver:profile:edit_phone"),
        _row("🚙 Данные автомобиля", "driver:profile:edit_car"),
        _row("📎 Загрузить документы", "driver:docs"),
        _row("📊 Статистика", "driver:stats"),
        _row("📜 История заказов", "driver:orders_history"),
    ]
    if not balance_hidden():
        rows.append(_row("💰 Баланс", "driver:balance"))
    rows.extend([
        _row("ℹ️ Помощь", "driver:help"),
        _row("🔄 Сменить роль", "menu:roles"),
        _row("↪️ Главное меню", "driver:menu"),
    ])
    return rows


def car_menu_rows() -> List[List[Dict[str, Any]]]:
    return [
        _row("🚙 Марка и модель", "driver:car:edit_model"),
        _row("🔢 Госномер", "driver:car:edit_number"),
        _row("🎨 Цвет", "driver:car:edit_color"),
        _row("↪️ Назад к профилю", "driver:profile"),
    ]


def docs_rows(include_main: bool = True) -> List[List[Dict[str, Any]]]:
    rows = [
        _row("📸 Селфи", "driver:docs:selfie"),
        _row("🚗 Фото авто (слева)", "driver:docs:car:left"),
        _row("🚗 Фото авто (справа)", "driver:docs:car:right"),
        _row("🚗 Фото авто (спереди)", "driver:docs:car:front"),
        _row("🚗 Фото авто (сзади)", "driver:docs:car:back"),
        _row("🛋 Фото салона", "driver:docs:car:interior"),
    ]
    if include_main:
        rows.append(_row("↪️ Главное меню", "driver:menu"))
    return rows


def registration_rows() -> List[List[Dict[str, Any]]]:
    return [_row("📝 Заполнить профиль", "driver:start_registration")]













def back_to_main_rows() -> List[List[Dict[str, Any]]]:
    return [_row("↪️ Главное меню", "driver:menu")]









    rows.append(_row("↪️ Главное меню", "driver:menu"))
    return rows


def moderation_block_rows(user_id: int, status: str) -> List[List[Dict[str, Any]]]:
    if status == "pending":
        return [
            _row("📝 Обновить данные", "driver:profile"),
            _row("📎 Загрузить документы", "driver:docs"),
            _row("↪️ Главное меню", "driver:menu"),
        ]
    if status == "rejected":
        return [
            _row("📝 Обновить данные", "driver:profile"),
            _row("📎 Загрузить документы", "driver:docs"),
            _row("🔁 Подать на модерацию", "driver:resubmit_moderation"),
            _row("↪️ Главное меню", "driver:menu"),
        ]
    return back_to_main_rows()


# ==================== Вспомогательные функции ====================
def is_moderation_required(user_id: int) -> bool:
    status = S.get_driver_verification_status(user_id)
    return status in ("pending", "rejected", "")


def check_moderation_or_none(user_id: int) -> Optional[Tuple[str, List[List[Dict[str, Any]]]]]:
    """Если водитель на модерации — вернуть (текст, клавиатура), иначе None."""
    status = S.get_driver_verification_status(user_id)
    if status == "pending":
        return ("🔒 Доступ ограничен. Профиль ожидает одобрения администратором.",
                moderation_block_rows(user_id, "pending"))
    if status == "rejected":
        return ("❌ Профиль отклонен. Обновите данные и подайте заново.",
                moderation_block_rows(user_id, "rejected"))
    return None


def time_based_greeting() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:
        return "🌅 Доброе утро!"
    if 12 <= h < 17:
        return "☀️ Добрый день!"
    if 17 <= h < 22:
        return "🌆 Добрый вечер!"
    return "🌙 Доброй ночи!"


def _ago_text(user_id: int, ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    except Exception:
        return ""
    diff = (datetime.now() - dt).total_seconds()
    if diff < 60:
        return "только что"
    if diff < 3600:
        return f"{int(diff / 60)} мин назад"
    if diff < 86400:
        return f"{int(diff / 3600)} ч назад"
    return f"{int(diff / 86400)} дн назад"


def driver_main_header(user_id: int) -> str:
    """Шапка главного меню: статус, доступ, город, адрес, время."""
    u = S.get_user(user_id) or {}
    status = u.get("status", "offline")
    status_text = {"online": "🟢 Онлайн", "offline": "🔴 Оффлайн", "break": "⏸ Перерыв"}.get(status, f"🔴 {status}")
    cfg_comm = S.get_commission_config()
    if not cfg_comm.get("enabled"):
        access_text = "✅ Доступ полный (бесплатный период)"
    elif S.has_active_access(user_id):
        exp = S.get_access_expires(user_id)
        access_text = f"✅ Доступ до {exp.strftime('%d.%m %H:%M')}" if exp else "✅ Доступ активен"
    else:
        access_text = "❌ Доступ неактивен"
    loc = u.get("last_location")
    loc = loc if isinstance(loc, dict) else {}
    city = loc.get("city", "") if isinstance(loc, dict) else ""
    current_city = u.get("current_city", "") if isinstance(u, dict) else ""
    address = loc.get("address", "") if isinstance(loc, dict) else ""
    ago = _ago_text(user_id, loc.get("updated_at"))
    name = S.get_driver_name(user_id) or u.get("first_name") or "Пользователь"
    return (
        f"👋 {time_based_greeting()}\n\n"
        f"{status_text}\n{access_text}\n\n"
        f"🏙 Город: {city or current_city or 'не определён'}\n"
        f"📍 Адрес: {address or '—'}\n"
        f"⏱ Обновлено: {ago or 'никогда'}\n\n"
        f"👤 {name}\n\n"
        "Выберите действие:"
    )


def generate_navigation_url(order: Dict[str, Any], phase: str = "to_pickup",
                            include_waypoints: bool = True) -> str:
    """URL Яндекс.Карт для навигации (подача → назначение с промежуточными точками)."""
    coords = []
    if phase == "to_pickup" and order.get("pickup_coords"):
        coords.append(order["pickup_coords"])
        if order.get("destination_coords") and include_waypoints:
            for wp in order.get("waypoints") or []:
                if isinstance(wp, dict) and wp.get("latitude") and wp.get("longitude"):
                    coords.append({"latitude": wp["latitude"], "longitude": wp["longitude"]})
            coords.append(order["destination_coords"])
    else:
        coords.append(order.get("destination_coords") or order.get("pickup_coords") or {}
)
    lat_lon = []
    for c in coords:
        lat = c.get("latitude") or c.get("lat")
        lon = c.get("longitude") or c.get("lon")
        if lat and lon:
            lat_lon.append(f"{lat},{lon}")
    if not lat_lon:
        return "https://yandex.ru/maps"
    return f"https://yandex.ru/maps/?rtt=auto&rtext={'~'.join(lat_lon)}&rview=0&mode=driving"


def calculate_haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    lat1r, lon1r = math.radians(lat1), math.radians(lon1)
    lat2r, lon2r = math.radians(lat2), math.radians(lon2)
    dlat, dlon = lat2r - lat1r, lon2r - lon1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def fmt_price(value: Any) -> str:
    try:
        return f"{float(value):.1f}₽"
    except (TypeError, ValueError):
        return "0₽"


def order_management_rows(order_id: int, order: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    rows: List[List[Dict[str, Any]]] = []
    status = order.get("status")
    if order_id and status in ("accepted", "driver_arrived", "in_progress"):
        if status == "accepted":
            # Menu at "на месте" (after accepting order): "Я прибыл, Навигация, Связаться с пассажиром, Отменить заказ"
            rows.append(_row("📍 Я прибыл", f"driver:arrived:{order_id}"))
            rows.append(_row("🗺 Навигация", f"driver:yandex_nav:{order_id}"))
            rows.append(_row("💬 Связаться с пассажиром", f"driver:contact:{order_id}"))
            rows.append(_row("❌ Отменить заказ", f"driver:cancel_order:{order_id}"))
        if status == "driver_arrived":
            # Menu after pressing "Я прибыл": "Начать движение, Связь с пассажиром, Отменить заказ, Список заказов"
            rows.append(_row("🚗 Начать движение", f"driver:start_driving:{order_id}"))
            rows.append(_row("💬 Связаться с пассажиром", f"driver:contact:{order_id}"))
            rows.append(_row("❌ Отменить заказ", f"driver:cancel_order:{order_id}"))
            rows.append(_row("📋 Список заказов", "driver:orders_available"))
        if status == "in_progress":
            # Menu "поехали": "Завершить поездку, Начать/Остановить ожидание, Навигация, Текущий заказ"
            rows.append(_row("✅ Завершить поездку", f"driver:complete_trip:{order_id}"))
            if order.get("waiting_started_at"):
                rows.append(_row("⏹ Остановить ожидание", f"driver:wait_stop:{order_id}"))
            else:
                rows.append(_row("⏱ Начать ожидание", f"driver:wait_start:{order_id}"))
            rows.append(_row("🗺 Навигация", f"driver:yandex_nav:{order_id}"))
            rows.append(_row("📋 Текущий заказ", "driver:current_order"))

    return rows


# ==================== Карточка заказа (единый текст) ====================
def format_waiting_line(info: Dict[str, Any]) -> str:
    """Строка статуса ожидания: время + бесплатный остаток или сумма."""
    if info["is_free"]:
        return (f"⏳ Ожидание: {info['waiting_time_text']} "
                f"(бесплатно, осталось {info['free_minutes_left']} мин)")
    return f"⏳ Ожидание: {info['waiting_time_text']} (+{fmt_price(info['cost'])})"


def build_order_card_text(order: Dict[str, Any], show_waiting: bool = True) -> str:
    """Текст карточки заказа для водителя (единый формат для всех экранов).

    Устойчив к обоим форматам хранения адресов: ``pickup_address``/``pickup``
    и ``dropoff_address``/``destination`` (старые заказы используют второй).
    """
    oid = order.get("id", 0)
    status = order.get("status", "")
    pickup = order.get("pickup_address") or order.get("pickup") or "—"
    dest = order.get("dropoff_address") or order.get("destination") or "по укозанию"
    try:
        distance_txt = f"{float(order.get('distance', 0) or 0):.1f} км"
    except (TypeError, ValueError):
        distance_txt = "—"
    labels = {"accepted": "✅ Принят — движемся к пассажиру",
              "driver_arrived": "🚗 Прибыл на точку",
              "in_progress": "🟢 В пути"}
    lines = [
        f"📋 <b>Заказ #{oid}</b>\n",
        f"📍 Откуда: {pickup}",
        f"🎯 Куда: {dest}",
        f"💰 Цена: {fmt_price(order.get('price', 0))}",
        f"📏 Расстояние: {distance_txt}",
        "",
        f"Статус: {labels.get(status, status)}",
    ]
    if show_waiting and status in ("accepted", "driver_arrived", "in_progress"):
        info = get_waiting_info(oid)
        if (order.get("waiting_started_at") or order.get("waiting_ended_at")
                or order.get("waiting_seconds_total")):
            lines.append(format_waiting_line(info))
    return "\n".join(lines)


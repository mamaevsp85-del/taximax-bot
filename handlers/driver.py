# -*- coding: utf-8 -*-
"""Full driver handlers for MAX taxi bot — migrated from Telegram driver bot."""
from __future__ import annotations
import logging, re
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import settings
from core import storage
from core.storage import haversine_distance, get_available_orders_for_driver
from core.driver_support import (
    STATE_REG_NAME, STATE_REG_CAR_MODEL, STATE_REG_CAR_NUMBER, STATE_REG_CAR_COLOR,
    STATE_AWAIT_LOCATION, STATE_AWAIT_FINAL_GEO, STATE_UPLOAD_SELFIE, STATE_UPLOAD_CAR_LEFT,
    STATE_UPLOAD_CAR_RIGHT, STATE_UPLOAD_CAR_FRONT, STATE_UPLOAD_CAR_BACK,
    STATE_UPLOAD_CAR_INTERIOR, STATE_EDIT_NAME, STATE_EDIT_PHONE,
    STATE_EDIT_CAR_MODEL, STATE_EDIT_CAR_NUMBER, STATE_EDIT_CAR_COLOR,
    STATE_BALANCE_TOPUP, STATE_STATS_CUSTOM, STATE_RELAY,
    add_order_to_driver_queue, get_driver_order_queue, get_driver_current_order,
    is_driver_busy, remove_order_from_driver_queue, is_moderation_required,
    check_moderation_or_none, driver_main_header, main_menu_rows, work_status_rows,
    access_rows, balance_rows, stats_rows, profile_rows, car_menu_rows, docs_rows,
    registration_rows, back_to_main_rows, balance_hidden, balance_rows_for_user,
    moderation_block_rows, order_management_rows, get_waiting_info,
    _driver_is_admin_or_manager,
    calculate_waiting_cost, start_waiting, stop_waiting, cancel_waiting_ticker,
    generate_navigation_url, fmt_price, time_based_greeting, build_order_card_text,
    start_relay_session, get_active_relay_for_user, get_relay_partner_id,
    stop_relay_session, track_sent_message, clear_driver_chat_messages,
    get_last_menu_message_id, set_last_menu_message_id,
)
from core.fsm import get_context
from core.router import Router
from core.tariff_manager import calculate_trip_price
from core.geocoder import get_address_by_coordinates, calculate_distance_and_time
from max_api.keyboards import callback_button, link_button, request_geo_location_button
from ui.reply import Reply, sanitize_input, get_menu_mid, remember_menu_mid

logger = logging.getLogger(__name__)
S = storage.storage


class _Reply(Reply):
    """Reply для водителя: инлайн-меню (редактирует предыдущее сообщение)."""
    def __init__(self, client, user_id, **kwargs):
        kwargs.setdefault('edit_last', True)
        kwargs.setdefault('default_back', 'driver:menu')
        super().__init__(client, user_id, **kwargs)


def _ask(client, user_id):
    """Запрос текста, гео или фото: новое сообщение, меню водителя не затирает."""
    return _Reply(client, user_id, edit_last=False, track_menu=False)

# FSM состояния для водителя
STATE_IDLE = 'STATE_IDLE'               # Свободен
STATE_ON_LINE = 'STATE_ON_LINE'         # На линии
STATE_ACTIVE_ORDER = 'STATE_ACTIVE_ORDER'  # Есть активный заказ
STATE_ORDER_LIST_VIEW = 'STATE_ORDER_LIST_VIEW'  # Просмотр списка заказов

# ==================== КАРТОЧКА АКТИВНОГО ЗАКАЗА ====================
def _build_active_order_card(order):
    'Build active order card text for driver.'
    status_map = {
        'accepted': 'Выезжаю к пассажиру',
        'driver_arrived': 'Пассажир на месте, выключаю сигнал',
        'in_progress': 'Поездка идет',
        'completed': 'Заказ завершён'
    }
    status_text = status_map.get(order.get('status', ''), order.get('status', ''))
    pickup = order.get('pickup', '')
    destination = order.get('destination', '')
    distance = order.get('distance', 0)
    price = order.get('price', 0)
    passenger_phone = order.get('passenger_snapshot', {}).get('phone', '')
    card = (
        '🚕 <b>Заказ #' + str(order.get('id', '')) + '</b> принят\n'
        '📍 Откуда: ' + str(pickup) + '\n'
        '🏁 Куда: ' + str(destination) + '\n'
        '📏 Расстояние: ' + str(distance) + ' км\n'
        '👤 Пассажир: ' + str(passenger_phone) + '\n'
        '💰 Стоимость: ' + str(price) + ' ₽\n\n'
        'Статус: <b>' + str(status_text) + '</b>'
    )
    return card


# ==================== Вспомогательные функции ====================
def _payload(update) -> str:
    cb = getattr(update, "callback", None)
    return (cb.payload or "") if cb else ""


def _cb_id_val(update, prefix: str) -> Optional[str]:
    p = _payload(update)
    return p[len(prefix):] if p.startswith(prefix) else None


async def _answer_cb(client, update) -> None:
    cb = getattr(update, "callback", None)
    if cb and cb.callback_id:
        try:
            await client.answer_callback(cb.callback_id)
        except Exception:
            pass


async def _send_menu(client, user_id, text, keyboard, replace_previous=0, edit_id=None):
    reply = _Reply(client, user_id)
    if edit_id:
        logger.info("DEBUG _send_menu user=%s EDITING mid=%s", user_id, edit_id)
        result = await reply.edit(edit_id, text, keyboard=keyboard)
        if result is None:
            logger.info("DEBUG _send_menu user=%s EDIT FAILED -> sending new", user_id)
            result = await reply.send(text, keyboard=keyboard, replace_previous=replace_previous)
    else:
        logger.info("DEBUG _send_menu user=%s SENDING (edit_last=True auto-edit)", user_id)
        result = await reply.send(text, keyboard=keyboard, replace_previous=replace_previous)
    if result:
        for key in ("message_id", "mid"):
            mid = result.get(key) if isinstance(result, dict) else None
            if mid:
                set_last_menu_message_id(user_id, str(mid))
                remember_menu_mid(user_id, str(mid))
                logger.info("DEBUG _send_menu user=%s new mid=%s", user_id, mid)
                break
    return result


def _row(text, payload):
    return [callback_button(text, payload)]


def _access_hours(user_id: int) -> float:
    exp = S.get_access_expires(user_id)
    if not exp:
        return 0.0
    try:
        dt = datetime.fromisoformat(exp) if isinstance(exp, str) else exp
        return max(0.0, (dt - datetime.now()).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def _bl(user_id: int) -> Optional[str]:
    """Баланс как строка — или None, если скрыт админом."""
    if balance_hidden():
        return None
    return fmt_price(S.get_balance(user_id))


def _docs_status_text(user_id: int) -> str:
    u = S.get_user(user_id) or {}
    dk = {"selfie_photo_token": "Селфи", "car_photo_left_id": "Бок (слева)",
          "car_photo_right_id": "Бок (справа)", "car_photo_front_id": "Спереди",
          "car_photo_back_id": "Сзади", "car_photo_interior_id": "Интерьер"}
    lines = ["📎 <b>Документы для модерации</b>"]
    loaded = 0
    for k, lbl in dk.items():
        if u.get(k):
            lines.append(f"✅ {lbl}")
            loaded += 1
        else:
            lines.append(f"❌ {lbl}")
    lines.append(f"Загружено: {loaded}/6")
    return "\n".join(lines)


_DOC_STATE_TO_KEY = {
    STATE_UPLOAD_SELFIE: "selfie_photo_token",
    STATE_UPLOAD_CAR_LEFT: "car_photo_left_id",
    STATE_UPLOAD_CAR_RIGHT: "car_photo_right_id",
    STATE_UPLOAD_CAR_FRONT: "car_photo_front_id",
    STATE_UPLOAD_CAR_BACK: "car_photo_back_id",
    STATE_UPLOAD_CAR_INTERIOR: "car_photo_interior_id",
}

_DOC_NAMES = {
    STATE_UPLOAD_SELFIE: "селфи",
    STATE_UPLOAD_CAR_LEFT: "бок слева",
    STATE_UPLOAD_CAR_RIGHT: "бок справа",
    STATE_UPLOAD_CAR_FRONT: "спереди",
    STATE_UPLOAD_CAR_BACK: "сзади",
    STATE_UPLOAD_CAR_INTERIOR: "интерьер",
}

_STAT_PERIOD_LABEL = {"today": "сегодня", "day": "сегодня",
                      "week": "неделя", "month": "месяц", "all": "всё время",
                      "general": "всё время"}


def _period_label(key: str) -> str:
    return _STAT_PERIOD_LABEL.get(key, str(key))


def _stat_period_key(key: str) -> str:
    return {"day": "today", "week": "week", "month": "month"}.get(key, "today")


def _compute_custom_stats(user_id, d_start, d_end):
    completed = cancelled = 0
    earnings = 0.0
    for o in S.get_orders_by_driver(user_id):
        ts = o.get("completed_at") or o.get("accepted_at") or o.get("created_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        except Exception:
            continue
        if not (d_start <= dt <= d_end):
            continue
        status = o.get("status")
        if status == "completed":
            completed += 1
            earnings += float(o.get("price", 0) or 0)
        elif status in ("cancelled", "declined"):
            cancelled += 1
    return {"completed": completed, "cancelled": cancelled, "earnings": earnings}


def _profile_text(user_id) -> str:
    u = S.get_user(user_id) or {}
    ver = S.get_driver_verification_status(user_id)
    ver_label = {"approved": "✅ Одобрен", "pending": "⏳ На проверке",
                 "rejected": "❌ Отклонён"}.get(ver, "—")
    access_h = _access_hours(user_id)
    rating = float(u.get("rating", 5.0))
    completed = u.get("orders_completed", 0)
    bl_text = _bl(user_id)
    lines = [
        "👤 <b>Профиль водителя</b>\n",
        f"Имя: {S.get_driver_name(user_id) or '—'}",
        f"Телефон: {S.get_driver_phone(user_id) or 'не указан'}",
        f"Логин: @{u.get('username', '—')}\n",
        f"🚙 Авто: {u.get('car_model', '—')} {u.get('car_number', '')}",
        f"   Цвет: {u.get('car_color', '—')}\n",
    ]
    if bl_text:
        lines.append(f"💰 Баланс: {bl_text}")
    lines.append(f"🕒 Доступ: {access_h:.1f} ч")
    lines.append(f"⭐ Рейтинг: {rating:.1f}  ({completed} поездок)\n")
    lines.append(f"Модерация: {ver_label}")
    return "\n".join(lines)


# ==================== /START И РЕГИСТРАЦИЯ ====================
async def driver_start(client, user_id, update, ctx):
    """Точка входа: /start → проверка модерации → регистрация или главное меню."""
    fsm = get_context(user_id)
    await fsm.clear()
    u = update.user
    upd_name = u.name or u.username if u else ""
    upd_username = u.username if u else None
    if upd_name and not (S.get_user(user_id) or {}).get("name"):
        S.update_user(user_id, name=upd_name)
    if upd_username and not (S.get_user(user_id) or {}).get("username"):
        S.update_user(user_id, username=upd_username)

    ver_status = S.get_driver_verification_status(user_id)
    if ver_status == "approved":
        await _show_main_menu(client, user_id, update)
        return
    if ver_status == "pending":
        await _Reply(client, user_id).send(
            "📝 Заявка отправлена. Пождите проверки администратором.",
            keyboard=back_to_main_rows())
        return

    has_username = bool(u.username) if u else False
    name_preview = u.name or "Водитель" if u else "Водитель"
    username_preview = f"@{u.username}" if has_username else "—"
    welcome = (
        "👋 Добро пожаловать в ТаксиМакс для водителей!\n\n"
        f"Имя: {sanitize_input(name_preview)}\n"
        f"Логин: {username_preview}\n\n"
        "Для регистрации нужен юзернейм (логин) в MAX.\n"
        "Если его нет — укажите его в профиле, затем нажмите кнопку.\n\n"
        "Когда будете готовы — продолжим."
    )
    rows = [_row("👉 Далее", "driver:next_car_model") if has_username
            else _row("👉 Я указал логин", "driver:confirm_username")]
    await _Reply(client, user_id).send(welcome, keyboard=rows)


async def driver_next_car_model(client, user_id, update, ctx):
    """Начало регистрации: кнопка «Далее» после /start."""
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    u = update.user
    await fsm.update_data(
        name=u.name or "Водитель" if u else "Водитель",
        username=(u.username or f"user_{user_id}") if u else f"user_{user_id}",
    )
    await _Reply(client, user_id).send(
        "🚙 <b>Регистрация водителя</b>\n\nВведите марку и модель автомобиля "
        "(например: Toyota Camry):")
    await fsm.set_state(STATE_REG_CAR_MODEL)


async def driver_confirm_username(client, user_id, update, ctx):
    """Пользователь подтверждает, что указал логин."""
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    u = update.user or (update.callback.user if update.callback else None)
    username = (u.username or f"user_{user_id}") if u else f"user_{user_id}"
    if not (u and u.username):
        await _Reply(client, user_id).send(
            "❌ Логин всё ещё не найден. Укажите @логин в профиле MAX, затем нажмите снова.")
        return
    await fsm.update_data(
        name=(u.name or "Водитель") if u else "Водитель",
        username=username)
    await _Reply(client, user_id).send(
        f"✅ Логин: @{username}\n\nВведите марку и модель автомобиля:")
    await fsm.set_state(STATE_REG_CAR_MODEL)


async def driver_start_registration(client, user_id, update, ctx):
    """Кнопка «Заполнить профиль» из меню регистрации/модерации."""
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    u = S.get_user(user_id) or {}
    await fsm.update_data(
        name=u.get("name") or u.get("first_name") or "",
        username=u.get("username") or "",
    )
    await _ask(client, user_id).send("🚙 <b>Регистрация водителя</b>\n\nВведите ваше имя:")
    await fsm.set_state(STATE_REG_NAME)


async def reg_name(client, user_id, update, ctx):
    """Шаг: ввод имени."""
    fsm = get_context(user_id)
    text = (update.message.text or "").strip() if update.message else ""
    name = sanitize_input(text)
    if len(name) < 2:
        await _ask(client, user_id).send("❌ Имя должно быть минимум 2 символа. Попробуйте снова:")
        return
    fsm_data = await fsm.update_data(name=name)
    await _ask(client, user_id).send(f"✅ Имя: <b>{name}</b>\n\nВведите марку и модель авто:")
    await fsm.set_state(STATE_REG_CAR_MODEL)


# ==================== ГЛАВНОЕ МЕНЮ ====================
async def _show_main_menu(client, user_id, update=None, state_clear=True):
    """Показать главное меню с актуальной шапкой."""
    if state_clear:
        await get_context(user_id).clear()
    cur = get_driver_current_order(user_id)
    if cur:
        cancel_waiting_ticker(cur)
    text = driver_main_header(user_id)
    prev_mid = get_menu_mid(user_id) or get_last_menu_message_id(user_id)
    await _send_menu(client, user_id, text, main_menu_rows(user_id),
                     replace_previous=1, edit_id=prev_mid)


async def driver_menu(client, user_id, update, ctx):
    """Главное меню (entry point из role selector или /start)."""
    await _answer_cb(client, update)
    await _show_main_menu(client, user_id, update)


async def main_menu_cb(client, user_id, update, ctx):
    """«Главное меню» — возврат из любого подменю."""
    await _answer_cb(client, update)
    await _show_main_menu(client, user_id, update)


# ==================== СТАТУС (ЛИНИЯ) ====================
async def driver_work_status(client, user_id, update, ctx):
    """Меню статуса работы."""
    await _answer_cb(client, update)
    await _send_menu(client, user_id, "🚗 Статус работы\n\nВыберите статус:",
                     work_status_rows(), replace_previous=1)


async def driver_set_status(client, user_id, update, ctx):
    """Выйти на линию / уйти с линии / перерыв."""
    await _answer_cb(client, update)
    status = _payload(update).rsplit(":", 1)[-1]
    if S.is_driver_blocked(user_id):
        await _Reply(client, user_id).send(
            "⚠️ Ваш профиль временно заблокирован.",
            keyboard=back_to_main_rows())
        return
    if status == "online":
        if not S.has_active_access(user_id):
            await _Reply(client, user_id).send(
                "❌ Доступ неактивен. Купите часы.",
                keyboard=balance_rows_for_user(user_id))
            return
        S.set_online_status(user_id, "online")
        S.update_user(user_id, last_active=datetime.now().isoformat())
        await _ask(client, user_id).send(
            "✅ Вы вышли на линию. Теперь вы получаете заказы.\n\n"
            "💡 Отправьте геопозицию кнопкой ниже — так быстрее найдут вас.",
            keyboard=[[request_geo_location_button("📍 Отправить геопозицию", quick=True)],
                      [callback_button("↩️ Меню", "driver:menu")]])
        await get_context(user_id).set_state(STATE_AWAIT_LOCATION)
        return
    if status == "offline":
        S.set_online_status(user_id, "offline")
        S.update_user(user_id, last_active=datetime.now().isoformat())
        await get_context(user_id).set_state(STATE_IDLE)
    elif status == "break":
        S.set_online_status(user_id, "break")
        S.update_user(user_id, last_active=datetime.now().isoformat())
        await get_context(user_id).set_state(STATE_IDLE)
    label = {"online": "в сети", "offline": "офлайн", "break": "в перерыве"}.get(status, status)
    await _Reply(client, user_id).send(f"✅ Статус изменён: {label}.",
                                       keyboard=back_to_main_rows())


# ==================== МОДЕРАЦИЯ / ОЖИДАНИЕ ====================
async def driver_wait_admin(client, user_id, update, ctx):
    """Отправить заявку на модерацию."""
    await _answer_cb(client, update)
    if S.get_driver_verification_status(user_id) != "pending":
        S.set_driver_verification_status(user_id, "pending")
    await _Reply(client, user_id).send(
        "📝 Заявка отправлена. Пождите проверки администратором.",
        keyboard=back_to_main_rows())


async def driver_chat_queue(client, user_id, update, ctx):
    """Подсказка: подтверждение через чат водителей."""
    await _answer_cb(client, update)
    await _Reply(client, user_id).send(
        "📎 <b>Подтверждение через чат водителей</b>\n\n"
        "Попросите участника чата написать: ваше имя, номер авто.\n"
        "Администратор свяжется с вами после проверки.",
        keyboard=back_to_main_rows())


async def driver_resubmit_moderation(client, user_id, update, ctx):
    """Повторно подать на модерацию."""
    await _answer_cb(client, update)
    if not S.is_driver_profile_complete(user_id):
        await _Reply(client, user_id).send("📝 Заполните профиль.", keyboard=profile_rows())
        return
    if not S.has_driver_docs(user_id):
        await _Reply(client, user_id).send("📎 Загрузите документы.", keyboard=docs_rows())
        return
    S.set_driver_verification_status(user_id, "pending")
    await _Reply(client, user_id).send(
        "📝 Заявка повторно отправлена. Ожидайте решения.",
        keyboard=back_to_main_rows())


    S.set_driver_verification_status(user_id, "pending")
    await _Reply(client, user_id).send(
        "📝 Заявка повторно отправлена. Ожидайте решения.",
        keyboard=back_to_main_rows())


# ==================== РЕГИСТРАЦИЯ — ПРОДОЛЖЕНИЕ ====================
async def reg_car_model(client, user_id, update, ctx):
    """Шаг: ввод марки/модели авто."""
    fsm = get_context(user_id)
    text = (update.message.text or "").strip() if update.message else ""
    model = sanitize_input(text)
    if len(model) < 2:
        await _ask(client, user_id).send("❌ Укажите марку и модель (минимум 2 символа):")
        return
    await fsm.update_data(car_model=model)
    await _ask(client, user_id).send("🔢 Введите госномер автомобиля:\nПример: А123БВ77")
    await fsm.set_state(STATE_REG_CAR_NUMBER)


async def reg_car_number(client, user_id, update, ctx):
    """Шаг: ввод госномера."""
    fsm = get_context(user_id)
    text = (update.message.text or "").strip() if update.message else ""
    number = text.upper()
    if len(number) < 3:
        await _ask(client, user_id).send("❌ Некорректный госномер. Попробуйте снова:")
        return
    await fsm.update_data(car_number=number)
    await _ask(client, user_id).send("🎨 Введите цвет автомобиля (например: Белый):")
    await fsm.set_state(STATE_REG_CAR_COLOR)


async def reg_car_color(client, user_id, update, ctx):
    """Шаг: ввод цвета → создание профиля, отправка на модерацию."""
    fsm = get_context(user_id)
    data = await fsm.get_data()
    text = (update.message.text or "").strip() if update.message else ""
    color = sanitize_input(text) or "Не указан"
    S.update_user(user_id,
                  name=data.get("name", "Водитель"),
                  driver_name=data.get("name", "Водитель"),
                  username=data.get("username"),
                  car_model=data.get("car_model", ""),
                  car_number=data.get("car_number", ""),
                  car_color=color,
                  driver_verification_status="pending",
                  active_role=storage.ROLE_DRIVER)
    await fsm.clear()
    instructions = (
        "✅ Профиль водителя создан!\n\n"
        f"👤 Имя: {data.get('name', 'Водитель')}\n"
        f"🚙 Авто: {data.get('car_model', '')} {data.get('car_number', '')} ({color})\n\n"
        "<b>Далее — подтверждение администратором.</b>\n\n"
        "1) Чат водителей — попросите друга‑водителя написать: «Имя» «номер авто».\n"
        f"2) Или напишите администратору: {S.admin_data.get('support_contact', settings.support_contact)}\n\n"
        "После подтверждения доступ будет активирован."
    )
    rows = [_row("📎 Чат водителей", "driver:chat_queue"),
            _row("📝 Ждать модерацию", "driver:wait_admin"),
            _row("↩️ Меню", "driver:menu")]
    await _Reply(client, user_id).send(instructions, keyboard=rows)


# ==================== ГЕОЛОКАЦИЯ ====================
async def driver_request_location(client, user_id, update, ctx):
    """Кнопка «Передать геопозицию»."""
    await _answer_cb(client, update)
    await _Reply(client, user_id).send(
        "📍 Пришлите свою геопозицию через кнопку ниже:",
        keyboard=[[request_geo_location_button("📍 Отправить локацию", quick=True)],
                  [callback_button("↩️ Меню", "driver:menu")]])
    await get_context(user_id).set_state(STATE_AWAIT_LOCATION)


async def driver_location_input(client, user_id, update, ctx):
    """State STATE_AWAIT_LOCATION — полученная локация."""
    fsm = get_context(user_id)
    msg = update.message
    loc = getattr(msg, "location", None)
    if loc is None:
        await _ask(client, user_id).send("❌ Не вижу координат. Пришлите локацию через кнопку.")
        return
    latitude = loc.latitude
    longitude = loc.longitude
    address, city = await get_address_by_coordinates(latitude, longitude)
    now = datetime.now().isoformat()
    S.update_user(
        user_id,
        last_location={
            "latitude": latitude,
            "longitude": longitude,
            "address": address or "",
            "city": city or "",
            "updated_at": now,
        },
        current_city=city or "",
        last_lat=latitude,
        last_lon=longitude,
        last_location_at=now,
    )
    cur_oid = get_driver_current_order(user_id)
    if cur_oid:
        order = S.get_order(cur_oid)
        passenger_id = order.get("passenger_id") if order else None
        if passenger_id:
            try:
                pass_int = int(passenger_id)
            except (TypeError, ValueError):
                pass_int = None
            if pass_int:
                await client.send_message(
                    user_id=pass_int,
                    text="🚗 Водитель прибыл на точку. Таймер ожидания запущен.")
        start_waiting(cur_oid)
    else:
        await _Reply(client, user_id).send(
            f"✅ Локация сохранена: {address or f'{latitude},{longitude}'}\n"
            f"Статус: {S.get_user(user_id).get('status', 'offline')}")
    await _show_main_menu(client, user_id, state_clear=False)


# ==================== ПРОФИЛЬ ====================
async def driver_profile(client, user_id, update, ctx):
    """Просмотр/управление профилем."""
    await _answer_cb(client, update)
    await _send_menu(client, user_id, _profile_text(user_id), profile_rows(), replace_previous=1)


async def driver_edit_name(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await _ask(client, user_id).send("Введите новое имя:")
    await fsm.set_state(STATE_EDIT_NAME)


async def driver_edit_phone(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await _ask(client, user_id).send("Введите телефон (формат: +7XXXXXXXXXX):")
    await fsm.set_state(STATE_EDIT_PHONE)


async def driver_edit_car_menu(client, user_id, update, ctx):
    await _answer_cb(client, update)
    await _send_menu(client, user_id, "🚗 Редактирование авто:", car_menu_rows(), replace_previous=1)


async def driver_edit_car_model(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await _ask(client, user_id).send("Введите марку и модель авто:")
    await fsm.set_state(STATE_EDIT_CAR_MODEL)


async def driver_edit_car_number(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await _ask(client, user_id).send("Введите госномер авто:")
    await fsm.set_state(STATE_EDIT_CAR_NUMBER)


async def driver_edit_car_color(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await _ask(client, user_id).send("Введите цвет авто:")
    await fsm.set_state(STATE_EDIT_CAR_COLOR)


# ==================== ОБРАБОТКА ВВОДА В ФОРМАХ ====================
async def process_edit_name(client, user_id, update, ctx):
    fsm = get_context(user_id)
    text = (update.message.text or "").strip() if update.message else ""
    name = sanitize_input(text)
    if len(name) < 2:
        await _ask(client, user_id).send("❌ Имя должно быть минимум 2 символа. Попробуйте снова:")
        return
    S.update_user(user_id, driver_name=name)
    await fsm.clear()
    await _Reply(client, user_id).send(f"✅ Имя изменено на: {name}")
    await _show_main_menu(client, user_id, state_clear=False)


async def process_edit_phone(client, user_id, update, ctx):
    fsm = get_context(user_id)
    text = (update.message.text or "").strip() if update.message else ""
    phone = re.sub(r"[^\d+]", "", text)
    if len(phone) < 10:
        await _ask(client, user_id).send("❌ Некорректный номер. Попробуйте снова:")
        return
    S.update_user(user_id, driver_phone=phone)
    await fsm.clear()
    await _Reply(client, user_id).send(f"✅ Телефон сохранён: {phone}")
    await _show_main_menu(client, user_id, state_clear=False)


async def process_edit_car_model(client, user_id, update, ctx):
    fsm = get_context(user_id)
    if await fsm.get_state() == STATE_REG_CAR_MODEL:
        await reg_car_model(client, user_id, update, ctx)
        return
    text = (update.message.text or "").strip() if update.message else ""
    model = sanitize_input(text)
    if len(model) < 2:
        await _ask(client, user_id).send("❌ Укажите марку и модель (минимум 2 символа):")
        return
    S.update_user(user_id, car_model=model)
    await fsm.clear()
    await _Reply(client, user_id).send(f"✅ Марка/модель: {model}")
    await _send_menu(client, user_id, _profile_text(user_id), profile_rows(), replace_previous=1)


async def process_edit_car_number(client, user_id, update, ctx):
    fsm = get_context(user_id)
    if await fsm.get_state() == STATE_REG_CAR_NUMBER:
        await reg_car_number(client, user_id, update, ctx)
        return
    text = (update.message.text or "").strip() if update.message else ""
    number = text.upper()
    if len(number) < 3:
        await _ask(client, user_id).send("❌ Некорректный номер. Попробуйте снова:")
        return
    S.update_user(user_id, car_number=number)
    await fsm.clear()
    await _Reply(client, user_id).send(f"✅ Госномер: {number}")
    await _send_menu(client, user_id, _profile_text(user_id), profile_rows(), replace_previous=1)


async def process_edit_car_color(client, user_id, update, ctx):
    fsm = get_context(user_id)
    if await fsm.get_state() == STATE_REG_CAR_COLOR:
        await reg_car_color(client, user_id, update, ctx)
        return
    text = (update.message.text or "").strip() if update.message else ""
    color = sanitize_input(text) or "Не указан"
    S.update_user(user_id, car_color=color)
    await fsm.clear()
    await _Reply(client, user_id).send(f"✅ Цвет: {color}")
    await _send_menu(client, user_id, _profile_text(user_id), profile_rows(), replace_previous=1)


# ==================== ДОКУМЕНТЫ ====================
async def driver_docs(client, user_id, update, ctx):
    """Меню документов."""
    await _answer_cb(client, update)
    if not S.is_driver_profile_complete(user_id):
        await _Reply(client, user_id).send(
            "📝 Заполните профиль и данные авто.", keyboard=profile_rows())
        return
    ver = S.get_driver_verification_status(user_id)
    text = f"📎 <b>Документы</b>\n\nСтатус: {ver}\n" + _docs_status_text(user_id)
    await _send_menu(client, user_id, text, docs_rows(), replace_previous=1)


async def driver_doc_upload_selfie(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await fsm.set_state(STATE_UPLOAD_SELFIE)
    await _ask(client, user_id).send("📸 Пришлите селфи (фото водителя):")


async def driver_doc_upload_car_left(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await fsm.set_state(STATE_UPLOAD_CAR_LEFT)
    await _ask(client, user_id).send("📸 Пришлите фото авто — бок (слева):")


async def driver_doc_upload_car_right(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await fsm.set_state(STATE_UPLOAD_CAR_RIGHT)
    await _ask(client, user_id).send("📸 Пришлите фото авто — бок (справа):")


async def driver_doc_upload_car_front(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await fsm.set_state(STATE_UPLOAD_CAR_FRONT)
    await _ask(client, user_id).send("📸 Пришлите фото авто — спереди:")


async def driver_doc_upload_car_back(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await fsm.set_state(STATE_UPLOAD_CAR_BACK)
    await _ask(client, user_id).send("📸 Пришлите фото авто — сзади:")


async def driver_doc_upload_car_interior(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await fsm.set_state(STATE_UPLOAD_CAR_INTERIOR)
    await _ask(client, user_id).send("📸 Пришлите фото интерьера (салона):")


async def driver_doc_received(client, user_id, update, ctx):
    """State STATE_UPLOAD_* — фото получено."""
    fsm = get_context(user_id)
    doc_type = await fsm.get_state()
    msg = update.message
    img = getattr(msg, "image", None)
    if img is None:
        await _ask(client, user_id).send("❌ Пришлите фотографию.")
        return
    file_id = img.image_token
    if file_id:
        doc_key = _DOC_STATE_TO_KEY.get(doc_type)
        if doc_key:
            S.update_user(user_id, **{doc_key: file_id})
        doc_name = _DOC_NAMES.get(doc_type, doc_type)
        await _Reply(client, user_id).send(f"✅ Документ «{doc_name}» сохранён.")
    else:
        await _Reply(client, user_id).send("❌ Не удалось получить фото.")
    await fsm.clear()
    await driver_docs(client, user_id, update, ctx)


# ==================== БАЛАНС / ДОСТУП ====================
async def driver_balance(client, user_id, update, ctx):
    await _answer_cb(client, update)
    if balance_hidden() and not _driver_is_admin_or_manager(user_id):
        await _send_menu(client, user_id,
                         "🔒 Управление балансом доступно только админу и менеджеру.",
                         back_to_main_rows(), replace_previous=1)
        return
    if balance_hidden():
        await _send_menu(client, user_id,
                         "🔕 Платные функции отключены администратором.",
                         back_to_main_rows(), replace_previous=1)
        return
    bal = S.get_balance(user_id)
    text = (f"💰 <b>Баланс: {fmt_price(bal)}</b>\n\n"
            f"🕒 Доступ: {_access_hours(user_id):.1f} ч\n\n"
            "Выберите действие:")
    await _send_menu(client, user_id, text, balance_rows_for_user(user_id), replace_previous=1)


async def driver_balance_topup(client, user_id, update, ctx):
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    if not _driver_is_admin_or_manager(user_id):
        await _Reply(client, user_id).send(
            "🔒 Пополнение доступно только админу и менеджеру.",
            keyboard=back_to_main_rows())
        return
    if balance_hidden():
        await _Reply(client, user_id).send(
            "🔕 Пополнение отключено администратором.",
            keyboard=back_to_main_rows())
        return
    bal = S.get_balance(user_id)
    await _Reply(client, user_id).send(
        f"💰 Введите сумму пополнения (₽):\n\n"
        f"Ваш баланс: {fmt_price(bal)}\n\nВведите сумму (минимум 100 ₽):")
    await fsm.set_state(STATE_BALANCE_TOPUP)


async def process_balance_topup(client, user_id, update, ctx):
    """STATE_BALANCE_TOPUP — подтверждение пополнения."""
    fsm = get_context(user_id)
    text = (update.message.text or "").strip() if update.message else ""
    try:
        amount = float(text.replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        await _ask(client, user_id).send("❌ Введите корректную сумму (минимум 100 ₽):")
        return
    if amount < 100:
        await _ask(client, user_id).send("❌ Минимальная сумма пополнения — 100 ₽:")
        return
    S.add_transaction(user_id, "topup", amount, description="Пополнение баланса")
    S.add_balance(user_id, amount)
    await fsm.clear()
    bl = _bl(user_id)
    balance_line = f" Текущий баланс: {bl}" if bl else ""
    await _Reply(client, user_id).send(
        f"✅ Баланс пополнен на {fmt_price(amount)}!{balance_line}")
    await driver_balance(client, user_id, update, ctx)


async def driver_balance_buy_access(client, user_id, update, ctx):
    """Купить часы доступа: payload driver:balance:buy:{hours}."""
    await _answer_cb(client, update)
    if balance_hidden():
        await _Reply(client, user_id).send(
            "🔕 Платные функции отключены администратором.\n"
            "Покупка смен временно недоступна.")
        return
    val = _cb_id_val(update, "driver:balance:buy:")
    try:
        hours = int(val)
    except (TypeError, ValueError):
        await _Reply(client, user_id).send("❌ Некорректное количество часов.")
        return
    tariffs = S.get_access_tariffs()
    if not tariffs:
        # Комиссия отключена — покупка смен недоступна.
        await _Reply(client, user_id).send(
            "🔔 Платные функции отключены администратором.\n"
            "Покупка смен временно недоступна.",
            keyboard=balance_rows(user_id))
        return
    if hours not in tariffs:
        await _Reply(client, user_id).send(
            "❌ Эта смена больше не продаётся.", keyboard=balance_rows_for_user(user_id))
        return
    price = tariffs[hours]
    bal = S.get_balance(user_id)
    if bal < price:
        balance_msg = f" Баланс: {fmt_price(bal)}" if _bl(user_id) else ""
        await _Reply(client, user_id).send(
            f"❌ Недостаточно средств."
            f" Нужно: {fmt_price(price)}|{balance_msg}\n"
            "Пополните баланс:", keyboard=balance_rows_for_user(user_id))
        return
    S.deduct_balance(user_id, price)
    S.add_access_hours(user_id, hours)
    S.add_transaction(user_id, "access_buy", price, hours=hours,
                      description=f"Доступ {hours}ч")
    bl = _bl(user_id)
    balance_line = f"\n💰 Баланс: {bl}" if bl else ""
    await _Reply(client, user_id).send(
        f"✅ Доступ куплен!\n"
        f"Добавлено: {hours} ч  ({fmt_price(price)})\n"
        f"Остаток доступа: {_access_hours(user_id):.1f} ч{balance_line}",
        keyboard=balance_rows(user_id))


async def driver_balance_history(client, user_id, update, ctx):
    await _answer_cb(client, update)
    if not _driver_is_admin_or_manager(user_id):
        await _send_menu(client, user_id,
                         "🔒 Управление балансом доступно только админу и менеджеру.",
                         back_to_main_rows(), replace_previous=1)
        return
    if balance_hidden():
        await _send_menu(client, user_id,
                         "🔕 История операций скрыта администратором.",
                         back_to_main_rows(), replace_previous=1)
        return
    txns = S.get_transaction_history(user_id, limit=30)
    if not txns:
        text = "📄 История транзакций пуста."
    else:
        lines = ["📄 <b>История транзакций</b>"]
        for t in txns:
            sign = "+" if t.get("type") == "topup" else "−"
            lines.append(f"{sign} {fmt_price(float(t.get('amount', 0)))} — "
                         f"{t.get('type', '?')}")
        text = "\n".join(lines)
    await _send_menu(client, user_id, text, balance_rows(user_id), replace_previous=1)


# ==================== СТАТИСТИКА ====================
def _period_label(key: str) -> str:
    return {"today": "сегодня", "day": "сегодня", "week": "неделя",
            "month": "месяц", "all": "всё время",
            "general": "всё время"}.get(key, str(key))


def _stat_period_key(key: str) -> str:
    """Map driver callback period → storage period string."""
    return {"day": "today", "week": "week", "month": "month"}.get(key, "today")


def _compute_custom_stats(user_id, d_start, d_end):
    """Подсчёт статистики за произвольный диапазон дат."""
    completed = cancelled = 0
    earnings = 0.0
    for o in S.get_orders_by_driver(user_id):
        ts = o.get("completed_at") or o.get("accepted_at") or o.get("created_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        except Exception:
            continue
        if not (d_start <= dt <= d_end):
            continue
        status = o.get("status")
        if status == "completed":
            completed += 1
            earnings += float(o.get("price", 0) or 0)
        elif status in ("cancelled", "declined"):
            cancelled += 1
    return {"completed": completed, "cancelled": cancelled, "earnings": earnings}


async def driver_stats(client, user_id, update, ctx):
    await _answer_cb(client, update)
    await _send_menu(client, user_id, "📊 Статистика\n\nВыберите период:",
                     stats_rows(), replace_previous=1)


async def driver_stats_period(client, user_id, update, ctx, period_key: str):
    """Показать статистику за период (day/week/month/general)."""
    await _answer_cb(client, update)
    speriod = _stat_period_key(period_key)
    if period_key == "general":
        speriod = "all"
    stats = S.get_driver_period_stats(user_id, period=speriod)
    completed = stats.get("orders_completed", stats.get("count", 0))
    earnings = stats.get("earnings", 0.0)
    rating = stats.get("rating", 0.0)
    if not completed:
        text = ("📊 <b>Статистика</b>\n\n"
                "За выбранный период: 0 поездок.\n"
                "Начинайте работу, чтобы увидеть данные.")
    else:
        text = (f"📊 <b>Статистика ({_period_label(period_key)})</b>\n\n"
                f"✅ Выполнено: {completed}\n"
                f"💰 Заработано: {fmt_price(earnings)}\n"
                f"⭐ Рейтинг: {rating:.1f}")
    await _send_menu(client, user_id, text, stats_rows(), replace_previous=1)


async def driver_stats_day(client, user_id, update, ctx):
    await driver_stats_period(client, user_id, update, ctx, "day")


async def driver_stats_week(client, user_id, update, ctx):
    await driver_stats_period(client, user_id, update, ctx, "week")


async def driver_stats_month(client, user_id, update, ctx):
    await driver_stats_period(client, user_id, update, ctx, "month")


async def driver_stats_general(client, user_id, update, ctx):
    await driver_stats_period(client, user_id, update, ctx, "general")


async def driver_stats_custom(client, user_id, update, ctx):
    """Запросить диапазон дат для кастомной статистики."""
    await _answer_cb(client, update)
    fsm = get_context(user_id)
    await fsm.clear()
    await _Reply(client, user_id).send(
        "📊 Введите период в формате:\n\n"
        "<b>дд.мм.гггг-дд.мм.гггг</b>\n\n"
        "Пример: 01.10.2025-07.10.2025")
    await fsm.set_state(STATE_STATS_CUSTOM)


async def process_stats_custom(client, user_id, update, ctx):
    """STATE_STATS_CUSTOM — обработка кастомного диапазона дат."""
    fsm = get_context(user_id)
    text = (update.message.text or "").strip() if update.message else ""
    match = re.match(r"(\d{2}\.\d{2}\.\d{4})-(\d{2}\.\d{2}\.\d{4})", text)
    if not match:
        await _Reply(client, user_id).send(
            "❌ Формат: дд.мм.гггг-дд.мм.гггг\nПример: 01.10.2025-07.10.2025")
        return
    start_s, end_s = match.groups()
    try:
        d_start = datetime.strptime(start_s, "%d.%m.%Y")
        d_end = datetime.strptime(end_s, "%d.%m.%Y")
    except ValueError:
        await _Reply(client, user_id).send("❌ Некорректные даты. Попробуйте снова:")
        return
    if d_end < d_start:
        await _Reply(client, user_id).send("❌ Конец раньше начала.")
        return
    stats = _compute_custom_stats(user_id, d_start, d_end)
    text_out = (f"📊 <b>Статистика ({start_s} — {end_s})</b>\n\n"
                f"✅ Выполнно: {stats['completed']}\n"
                f"❌ Отменено: {stats.get('cancelled', 0)}\n"
                f"💰 Заработано: {fmt_price(stats.get('earnings', 0))}")
    await fsm.clear()
    await _send_menu(client, user_id, text_out, stats_rows(), replace_previous=1)


# ==================== ДОСТУПНЫЕ ЗАКАЗЫ ====================
async def driver_orders_available(client, user_id, update, ctx):
    """Список доступных заказов для водителя."""
    await _answer_cb(client, update)
    ver = S.get_driver_verification_status(user_id)
    if ver != "approved":
        block = check_moderation_or_none(user_id)
        if block:
            await _send_menu(client, user_id, block[0], block[1], replace_previous=1)
            return
    if not S.has_active_access(user_id):
        await _Reply(client, user_id).send(
            "❌ Доступ неактивен. Пополнение доступно только админу и менеджеру.",
            keyboard=back_to_main_rows())
        return
    if S.get_user(user_id).get("status") != "online":
        await _Reply(client, user_id).send("⚠️ Сначала выйдите на линию.",
                                           keyboard=work_status_rows())
        return
    if is_driver_busy(user_id):
        await _Reply(client, user_id).send(
            "🚗 У вас уже есть активный заказ.", keyboard=order_management_rows(0, {}))
        return
    pending = S.get_pending_orders()
    if not pending:
        await _send_menu(client, user_id,
                         "📋 На данный момент нет доступных заказов.\n\n"
                         "Вернитесь в меню и подождите.",
                         back_to_main_rows(), replace_previous=1)
        return
    rows = ["📋 <b>Список заказов:</b>\n"]
    kb: List[List[Dict[str, Any]]] = []
    for o in pending[:10]:
        oid = o.get("id", 0)
        pickup = o.get("pickup_address") or o.get("pickup", "—")
        dest = o.get("dropoff_address") or o.get("destination", "—")
        price = o.get("price", 0)
        distance = o.get("distance", "?")
        time_min = o.get("time_minutes", "?")
        rows.append(
            f"📍 {pickup} → {dest}\n"
            f"💰 {fmt_price(price)} | {distance} км | ⏱ {time_min} мин\n"
        )
        kb.append([callback_button(
            f"✅ {oid} — {pickup} → {dest}",
            f"driver:select_order:{oid}")])
    kb.append(_row("↪️ Главное меню", "driver:menu"))
    await _send_menu(client, user_id, "\n".join(rows), kb, replace_previous=1)
    await get_context(user_id).set_state(STATE_ORDER_LIST_VIEW)


async def _accept_order(client, user_id, oid: int) -> None:
    """Общая логика принятия заказа водителем (назначение + экран)."""
    if (S.get_driver_verification_status(user_id) != "approved"
            or (S.get_user(user_id) or {}).get("status") != "online"
            or not S.has_active_access(user_id)
            or S.is_driver_blocked(user_id)):
        await _Reply(client, user_id).send("❌ Принятие заказа недоступно. Проверьте статус и доступ.", keyboard=back_to_main_rows())
        return
    if is_driver_busy(user_id) or any(
        o.get("driver_id") == user_id and o.get("status") in ("accepted", "driver_arrived", "in_progress")
        for o in S.orders.values()
    ):
        await _Reply(client, user_id).send("🚗 У вас уже есть активный заказ.", keyboard=back_to_main_rows())
        return
    order = S.get_order(oid)
    if not order:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    if order.get("status") != "pending":
        await _Reply(client, user_id).send(
            "❌ Заказ уже занят.", keyboard=back_to_main_rows())
        return
    # Назначаем водителя
    S.update_order(oid, driver_id=user_id, status="accepted",
                   accepted_at=datetime.now().isoformat())
    S.update_user(user_id, driver_current_order_id=oid)
    add_order_to_driver_queue(user_id, oid)

    # Уведомляем пассажира, что к нему назначен водитель
    pid = order.get("passenger_id")
    if pid:
        try:
            pid_int = int(pid)
            du = S.get_user(user_id) or {}
            car = " ".join(filter(None, [du.get("car_model"), du.get("car_number")])).strip()
            text = (
                f"🚗 Ваш заказ #{oid} принял водитель!\n"
                f"👤 {du.get('name') or 'Водитель'}\n"
                + (f"🚘 {car}\n" if car else "")
                + "Ожидайте подачу по указанному адресу."
            )
            await client.send_message(user_id=pid_int, text=text)
            start_relay_session(user_id, pid_int, oid, hours=12.0)
        except (TypeError, ValueError):
            pass
        except Exception as e:
            logger.error("Не удалось уведомить пассажира о назначении водителя (заказ #%s): %s", oid, e)

    await _Reply(client, user_id).send(
        f"✅ Заказ #{oid} принят!\n\n"
        f"📍 Откуда: {order.get('pickup_address') or order.get('pickup', '—')}\n"
        f"   Куда: {order.get('dropoff_address') or order.get('destination', '—')}\n"
        f"💰 Цена: {fmt_price(order.get('price', 0))}\n\n"
        "Нажмите «Начать движение», когда поехали к пассажиру.",
        keyboard=order_management_rows(oid, order))
    await get_context(user_id).set_state(STATE_ACTIVE_ORDER)


async def driver_select_order(client, user_id, update, ctx):
    """Водитель выбрал заказ — назначаем его."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:select_order:")
    try:
        oid = int(oid_str)
    except (TypeError, ValueError):
        await _Reply(client, user_id).send("❌ Ошибка заказа.")
        return
    await _accept_order(client, user_id, oid)


async def driver_accept_order(client, user_id, update, ctx):
    """Принять заказ по кнопке из уведомления: payload 'order:accept:{id}'."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "order:accept:")
    try:
        oid = int(oid_str)
    except (TypeError, ValueError):
        await _Reply(client, user_id).send("❌ Ошибка заказа.")
        return
    await _accept_order(client, user_id, oid)


# ==================== ТЕКУЩИЙ ЗАКАЗ + ЖИЗНЕННЫЙ ЦИКЛ ====================
async def driver_current_order(client, user_id, update, ctx):
    """Текущий активный заказ."""
    await _answer_cb(client, update)
    oid = get_driver_current_order(user_id)
    if not oid:
        await _send_menu(client, user_id,
                         "📭 Нет активного заказа.\n\n📋 Список заказов →",
                         back_to_main_rows(), replace_previous=1)
        return
    order = S.get_order(oid)
    if not order:
        await _send_menu(client, user_id, "❌ Заказ не найден.",
                         back_to_main_rows(), replace_previous=1)
        return
    await get_context(user_id).set_state(STATE_ACTIVE_ORDER)
    card_text = _build_active_order_card(order)
    await _send_menu(client, user_id, card_text,
                     order_management_rows(oid, order), replace_previous=1)


def _order_text(order, show_waiting: bool = True) -> str:
    """Текст карточки заказа (единый формат, fallback адресов pickup/destination)."""
    return build_order_card_text(order, show_waiting=show_waiting)


async def _show_order_detail(client, user_id, oid, order, edit_id=None):
    text = _order_text(order, show_waiting=True)
    await _send_menu(client, user_id, text,
                     order_management_rows(oid, order), replace_previous=1,
                     edit_id=edit_id)


async def driver_start_driving(client, user_id, update, ctx):

    await _answer_cb(client, update)
    
    oid_str = _cb_id_val(update, "driver:start_driving:")
    try:
        oid = int(oid_str) if oid_str else get_driver_current_order(user_id)
    except (TypeError, ValueError):
        return
    if not oid:
        await _Reply(client, user_id).send("❌ Нет активного заказа.", keyboard=back_to_main_rows())
        return
    order = S.get_order(oid)
    if not order:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    if not order or order.get("driver_id") != user_id or order.get("status") not in ("accepted", "driver_arrived"):
        await _Reply(client, user_id).send("❌ Действие недоступно для этого заказа.", keyboard=back_to_main_rows())
        return
    stop_waiting(oid)
    S.update_order(oid, status="in_progress", started_at=datetime.now().isoformat())
    passenger_id = order.get("passenger_id")
    if passenger_id:
        try:
            pass_int = int(passenger_id)
        except (TypeError, ValueError):
            pass_int = None
        if pass_int:
            await client.send_message(
                user_id=pass_int,
                text="🚗 Водитель поехал. Ожидайте прибытие.")
    await _show_order_detail(client, user_id, oid, order)


async def driver_arrived(client, user_id, update, ctx):
    """Driver pressed "Я прибыл" — переход в статус driver_arrived."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:arrived:")
    try:
        oid = int(oid_str) if oid_str else get_driver_current_order(user_id)
    except (TypeError, ValueError):
        return
    if not oid:
        await _Reply(client, user_id).send("❌ Нет активного заказа.", keyboard=back_to_main_rows())
        return
    order = S.get_order(oid)
    if not order:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    if not order or order.get("driver_id") != user_id or order.get("status") not in ("accepted", "driver_arrived"):
        await _Reply(client, user_id).send("❌ Действие недоступно для этого заказа.", keyboard=back_to_main_rows())
        return
    S.update_order(oid, status="driver_arrived")
    await _Reply(client, user_id).send(
        f"🚗 <b>Заказ #{oid}</b>\n\n"
        f"Вы прибыли на точку пассажира.\n\n"
        f"Доступные действия:",
        keyboard=order_management_rows(oid, order)
    )

async def driver_yandex_nav(client, user_id, update, ctx):
    """Открыть навигацию (Yandex/Google) к точке."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:yandex_nav:")
    try:
        oid = int(oid_str) if oid_str else get_driver_current_order(user_id)
    except (TypeError, ValueError):
        oid = get_driver_current_order(user_id)
    order = S.get_order(oid) if oid else None
    if not order:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    nav_url = generate_navigation_url(order, phase="to_pickup")
    rows = [[link_button("🗺 Яндекс.Навигатор", nav_url)],
            [link_button("🗺 Google Maps", nav_url)],
            [callback_button("◀️ Назад", "driver:current_order")]]
    await _Reply(client, user_id).send("🗺 Навигация к точке подачи:", keyboard=rows)


async def driver_report_waiting(client, user_id, update, ctx):
    """Сообщить о прибытии → старт таймера ожидания."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:report_waiting:")
    try:
        oid = int(oid_str) if oid_str else get_driver_current_order(user_id)
    except (TypeError, ValueError):
        return
    if not oid:
        await _Reply(client, user_id).send("❌ Нет активного заказа.", keyboard=back_to_main_rows())
        return
    order = S.get_order(oid)
    if not order:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    if not order or order.get("driver_id") != user_id or order.get("status") not in ("accepted", "in_progress"):
        await _Reply(client, user_id).send("❌ Действие недоступно для этого заказа.", keyboard=back_to_main_rows())
        return
    S.update_order(oid, status="driver_arrived",
                   driver_arrived_at=datetime.now().isoformat())
    start_waiting(oid, client=client, driver_id=user_id,
                  passenger_id=order.get("passenger_id"))
    pid = order.get("passenger_id")
    if pid:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = None
        if pid_int:
            await client.send_message(
                user_id=pid_int,
                text=("Водитель прибыл и ожидает вас на точке подачи.\n\n"
                      "Первые 3 мин бесплатно, далее 5₽/мин — "
                      "вы получите уведомление, если начнётся платное ожидание."))
    await _show_order_detail(client, user_id, oid, order)


async def driver_wait_start(client, user_id, update, ctx):
    """Запустить платное ожидание."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:wait_start:") or str(get_driver_current_order(user_id) or "")
    try:
        oid = int(oid_str)
    except (TypeError, ValueError):
        return
    order = S.get_order(oid) if oid else None
    if not order or order.get("driver_id") != user_id or order.get("status") not in ("driver_arrived", "in_progress"):
        await _Reply(client, user_id).send("❌ Действие недоступно для этого заказа.", keyboard=back_to_main_rows())
        return
    if order.get("waiting_started_at") and not order.get("waiting_ended_at"):
        return
    start_waiting(oid, client=client, driver_id=user_id,
                  passenger_id=order.get("passenger_id"))
    await _show_order_detail(client, user_id, oid, S.get_order(oid) or {})


async def driver_wait_stop(client, user_id, update, ctx):
    """Остановить платное ожидание."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:wait_stop:") or str(get_driver_current_order(user_id) or "")
    try:
        oid = int(oid_str)
    except (TypeError, ValueError):
        return
    order = S.get_order(oid) if oid else None
    if not order or order.get("driver_id") != user_id or order.get("status") not in ("driver_arrived", "in_progress"):
        await _Reply(client, user_id).send("❌ Действие недоступно для этого заказа.", keyboard=back_to_main_rows())
        return
    stop_waiting(oid)
    await _show_order_detail(client, user_id, oid, S.get_order(oid) or {})


async def _update_driver_location_to_destination(user_id: int, order: Dict[str, Any]) -> None:
    """Обновить гео водителя на координаты завершения заказа."""
    coords = order.get("destination_coords")
    if not isinstance(coords, dict):
        return
    try:
        lat = float(coords.get("latitude") or coords.get("lat") or 0)
        lon = float(coords.get("longitude") or coords.get("lon") or 0)
    except (TypeError, ValueError):
        return
    if not lat or not lon:
        return
    address, city = await get_address_by_coordinates(lat, lon)
    now = datetime.now().isoformat()
    S.update_user(
        user_id,
        last_location={
            "latitude": lat,
            "longitude": lon,
            "address": address or "",
            "city": city or "",
            "updated_at": now,
        },
        current_city=city or "",
        last_lat=lat,
        last_lon=lon,
        last_location_at=now,
    )


async def driver_complete_trip(client, user_id, update, ctx):
    """Завершить поездку — запрашиваем геолокацию на точке высадки.

    Стоимость и расстояние пересчитываются по фактическим координатам
    водителя (см. driver_trip_final_geo).
    """
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:complete_trip:")
    try:
        oid = int(oid_str) if oid_str else get_driver_current_order(user_id)
    except (TypeError, ValueError):
        return
    if not oid:
        await _Reply(client, user_id).send("❌ Нет активного заказа.", keyboard=back_to_main_rows())
        return
    order = S.get_order(oid)
    if not order:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    if not order or order.get("driver_id") != user_id or order.get("status") not in ("in_progress", "driver_arrived"):
        await _Reply(client, user_id).send("❌ Действие недоступно для этого заказа.", keyboard=back_to_main_rows())
        return
    fsm = get_context(user_id)
    await fsm.clear()
    await fsm.update_data(final_geo_order_id=oid)
    await fsm.set_state(STATE_AWAIT_FINAL_GEO)
    await _ask(client, user_id).send(
        f"📍 <b>Завершение поездки #{oid}</b>\n\n"
        "Отправьте вашу геолокацию на точке высадки — по ней система\n"
        "пересчитает расстояние и стоимость поездки.",
        keyboard=[[request_geo_location_button("📍 Отправить геопозицию", quick=True)],
                  [callback_button("↩️ Меню", "driver:menu")]])


async def driver_trip_final_geo(client, user_id, update, ctx):
    """State STATE_AWAIT_FINAL_GEO — гео водителя: пересчёт стоимости и завершение."""
    fsm = get_context(user_id)
    msg = update.message
    loc = getattr(msg, "location", None)
    if loc is None or loc.latitude is None or loc.longitude is None:
        await _Reply(client, user_id).send(
            "❌ Не вижу координат. Пришлите локацию через кнопку ниже.",
            keyboard=[[request_geo_location_button("📍 Отправить геопозицию", quick=True)],
                      [callback_button("↩️ Меню", "driver:menu")]])
        return
    data = await fsm.get_data()
    try:
        oid = int(data.get("final_geo_order_id") or 0)
    except (TypeError, ValueError):
        oid = 0
    if not oid:
        oid = get_driver_current_order(user_id) or 0
    order = S.get_order(oid) if oid else None
    if not order or order.get("driver_id") != user_id or order.get("status") not in ("in_progress", "driver_arrived"):
        await fsm.clear()
        await _Reply(client, user_id).send("❌ Нет активного заказа для завершения.",
                                          keyboard=back_to_main_rows())
        return
    await _finalize_trip_with_geo(client, user_id, oid, float(loc.latitude), float(loc.longitude))
    await fsm.clear()
    await fsm.set_state(STATE_IDLE)


async def _finalize_trip_with_geo(client, user_id: int, oid: int,
                                  actual_lat: float, actual_lon: float) -> None:
    """Пересчёт по фактическим координатам высадки + завершение заказа."""
    order = S.get_order(oid)
    if not order:
        return
    old_distance = float(order.get("distance", 0) or 0)
    old_duration = int(order.get("time_minutes", 0) or 0)
    old_price = float(order.get("price", 0) or 0)

    # 1) Пересчёт расстояния/времени от точки подачи до фактической точки высадки
    new_distance, new_duration = old_distance, old_duration
    pickup_coords = order.get("pickup_coords") or {}
    if isinstance(pickup_coords, dict):
        # Координаты могут приходить в разных схемах ключей:
        #   geocoder / passenger_new  -> {"lat": .., "lon": ..}
        #   driver final geo          -> {"latitude": .., "longitude": ..}
        p_lat = pickup_coords.get("latitude", pickup_coords.get("lat"))
        p_lon = pickup_coords.get("longitude", pickup_coords.get("lon"))
    else:
        p_lat = p_lon = None
    try:
        p_lat = float(p_lat) if p_lat is not None else 0.0
        p_lon = float(p_lon) if p_lon is not None else 0.0
    except (TypeError, ValueError):
        p_lat = p_lon = 0.0
    if p_lat and p_lon:
        logger.info("Пересчёт поездки #%s: %.5f,%.5f -> %.5f,%.5f",
                    oid, p_lat, p_lon, actual_lat, actual_lon)
        calc = await calculate_distance_and_time(p_lat, p_lon, actual_lat, actual_lon)
        if calc and calc[0] is not None:
            new_distance = float(calc[0])
            new_duration = int(calc[1] or 0)
            logger.info("Расчёт: расстояние=%.1f км, время=%d мин", new_distance, new_duration)

    # 2) Фактический адрес точки высадки (обратный геокодинг)
    address, city = await get_address_by_coordinates(actual_lat, actual_lon)
    logger.info("Фактическая точка высадки #%s: %s, %s", oid, address, city)

    # 3) Пересчёт стоимости по тарифу
    new_price = calculate_trip_price(
        new_distance, new_duration,
        pickup_city=order.get("pickup_city"),
        destination_city=city or order.get("destination_city"),
    )
    logger.info("Стоимость #%s: старая=%.1f, новая=%.1f", oid, old_price, new_price)

    # 4) Обновляем заказ: фактическая точка назначения, расстояние, цена
    S.update_order(
        oid,
        destination=address or order.get("destination") or "",
        destination_city=city or order.get("destination_city") or "",
        destination_coords={"lat": actual_lat, "lon": actual_lon},
        distance=new_distance,
        time_minutes=new_duration,
        price=new_price,
        original_price=old_price,
        recalculated_at=datetime.now().isoformat(),
    )

    # 5) Отмечаем водителя на фактическом конечном адресе
    now = datetime.now().isoformat()
    S.update_user(
        user_id,
        last_location={
            "lat": actual_lat,
            "lon": actual_lon,
            "address": address or "",
            "city": city or "",
            "updated_at": now,
        },
        current_city=city or "",
        last_lat=actual_lat,
        last_lon=actual_lon,
        last_location_at=now,
    )

    # 6) Финализация заказа (ожидание, доход, статус)
    cancel_waiting_ticker(oid)
    stop_waiting(oid)
    waiting_cost = calculate_waiting_cost(oid)
    base_price = new_price
    total = base_price + waiting_cost
    commission = S.get_commission_config()
    if commission.get("enabled") and commission.get("mode") == "percent":
        pct = float(commission.get("percent", 0))
        driver_earning = total * (100 - pct) / 100.0
    else:
        driver_earning = total
    S.update_order(oid, status="completed",
                   completed_at=datetime.now().isoformat(),
                   waiting_cost=waiting_cost, final_price=total,
                   driver_earning=driver_earning, price=base_price)
    S.add_balance(user_id, driver_earning)
    S.add_transaction(user_id, "earning", driver_earning,
                      description=f"Поездка #{oid}")
    u = S.get_user(user_id) or {}
    S.update_user(user_id,
                  orders_completed=(u.get("orders_completed", 0) or 0) + 1,
                  earnings=(float(u.get("earnings", 0) or 0) + driver_earning),
                  driver_current_order_id=None,
                  last_active=datetime.now().isoformat())
    remove_order_from_driver_queue(user_id, oid)

    # 7) Детальный отчёт водителю и пассажиру
    dest_txt = address or order.get("destination") or "—"
    recalc_note = ""
    if abs(new_price - old_price) >= 0.5:
        direction = "📈" if new_price > old_price else "📉"
        recalc_note = (f"{direction} Стоимость пересчитана: {fmt_price(old_price)}"
                       f" → {fmt_price(new_price)}\n")
    details = (f"📍 {order.get('pickup') or order.get('pickup_address') or '—'}\n"
               f"🎯 {dest_txt}\n"
               f"📏 Расстояние: {new_distance:.1f} км\n"
               f"⏱ Время в пути: {new_duration} мин\n"
               + recalc_note +
               f"💰 Поездка: {fmt_price(base_price)}\n"
               + (f"⏳ Ожидание: {fmt_price(waiting_cost)}\n" if waiting_cost else "")
               + f"Итого: {fmt_price(total)}")
    pid = order.get("passenger_id")
    if pid:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = None
        if pid_int:
            kb = [[
                callback_button("⭐", f"passenger:rate:{oid}:1"),
                callback_button("⭐⭐", f"passenger:rate:{oid}:2"),
                callback_button("⭐⭐⭐", f"passenger:rate:{oid}:3"),
                callback_button("⭐⭐⭐⭐", f"passenger:rate:{oid}:4"),
                callback_button("⭐⭐⭐⭐⭐", f"passenger:rate:{oid}:5"),
            ],
                [callback_button("↪️ В меню", "passenger:menu")],
            ]
            await client.send_message(
                user_id=pid_int,
                text=f"✅ Поездка #{oid} завершена!\n\n{details}\n\nОцените водителя ⭐",
                attachments=[{"type": "inline_keyboard", "payload": {"buttons": kb}}])
            start_relay_session(user_id, pid_int, oid, hours=24.0)
    await _Reply(client, user_id).send(
        f"✅ Поездка #{oid} завершена!\n\n{details}\n"
        f"💵 Ваш заработок: {fmt_price(driver_earning)}\n"
        + (f"Баланс: {fmt_price(S.get_balance(user_id))}\n" if _bl(user_id) else ""),
        keyboard=back_to_main_rows())


async def driver_cancel_order(client, user_id, update, ctx):
    """Водитель отменяет заказ."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:cancel_order:")
    try:
        oid = int(oid_str) if oid_str else get_driver_current_order(user_id)
    except (TypeError, ValueError):
        oid = get_driver_current_order(user_id)
    order = S.get_order(oid) if oid else None
    if not order:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    if not order or (order.get("driver_id") not in (None, user_id)) or order.get("status") not in ("pending", "accepted", "driver_arrived", "in_progress"):
        await _Reply(client, user_id).send("❌ Действие недоступно для этого заказа.", keyboard=back_to_main_rows())
        return
    stop_waiting(oid)
    cancel_waiting_ticker(oid)
    was_assigned = order.get("driver_id") == user_id
    S.update_order(oid, status="cancelled",
                   cancelled_by="driver",
                   cancelled_at=datetime.now().isoformat())
    if was_assigned:
        S.update_user(user_id, driver_current_order_id=None)
        remove_order_from_driver_queue(user_id, oid)
        blocked = S.record_driver_cancel(user_id)
        if blocked:
            await _Reply(client, user_id).send(
                "⚠️ Вы временно заблокированы на 60 мин "
                "из‑за частых отмен.")
        else:
            await _Reply(client, user_id).send(
                "❌ Заказ отменён.\n\n"
                f"Серия отмен: {S.get_user(user_id).get('_cancel_count', 0)}")
    pid = order.get("passenger_id")
    if pid:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = None
        if pid_int:
            await client.send_message(
                user_id=pid_int,
                text="❌ Водитель отменил заказ.")
    await _show_main_menu(client, user_id, state_clear=False)


async def driver_contact_passenger(client, user_id, update, ctx):
    """Связаться с пассажиром (релей‑сессия или прямой ID)."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:contact:")
    try:
        oid = int(oid_str) if oid_str else get_driver_current_order(user_id)
    except (TypeError, ValueError):
        oid = get_driver_current_order(user_id)
    order = S.get_order(oid) if oid else None
    if not order:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    if not order or order.get("driver_id") != user_id or order.get("status") not in ("accepted", "driver_arrived", "in_progress"):
        await _Reply(client, user_id).send("❌ Действие недоступно для этого заказа.", keyboard=back_to_main_rows())
        return
    pid = order.get("passenger_id")
    if pid:
        # Открываем релей‑сессию на 15 минут
        start_relay_session(user_id, pid, oid, hours=0.25)
        await _Reply(client, user_id).send(
            "🔗 Релей‑соединение открыто на 15 минут.\n"
            "Пишите — пассажир получит ваши сообщения анонимно.",
            keyboard=back_to_main_rows())
    else:
        await _Reply(client, user_id).send("❌ Пассажир не найден.", keyboard=back_to_main_rows())


# ==================== ИСТОРИЯ ЗАКАЗОВ ====================
async def driver_orders_history(client, user_id, update, ctx):
    """История заказов водителя."""
    await _answer_cb(client, update)
    history = S.get_driver_history(user_id, limit=30)
    if not history:
        await _send_menu(client, user_id,
                         "📋 История заказов пуста.\n\n"
                         "Выполняйте заказы, чтобы увидеть их здесь.",
                         back_to_main_rows(), replace_previous=1)
        return
    lines = ["📋 <b>История заказов</b>\n"]
    rows: List[List[Dict[str, Any]]] = []
    for o in history[:15]:
        oid = o.get("id", 0)
        ts = o.get("completed_at") or o.get("accepted_at") or o.get("created_at", "")
        when = ts.split("T")[0] if ts else "—"
        status_emoji = {"completed": "✅", "cancelled": "❌"}.get(o.get("status"), "❓")
        lines.append(
            f"{status_emoji} #{oid}  {when}  "
            f"{fmt_price(o.get('price', 0))}")
        rows.append([callback_button(f"📄 #{oid}", f"driver:history_detail:{oid}")])
    rows.append(_row("↪️ Главное меню", "driver:menu"))
    await _send_menu(client, user_id, "\n".join(lines), rows, replace_previous=1)


async def driver_history_contact(client, user_id, update, ctx):
    """Написать пассажиру из истории (релей‑сессия)."""
    await _answer_cb(client, update)
    oid_str = _cb_id_val(update, "driver:history_detail:")
    try:
        oid = int(oid_str)
    except (TypeError, ValueError):
        await _Reply(client, user_id).send("❌ Ошибка.", keyboard=back_to_main_rows())
        return
    order = S.get_order(oid)
    if not order or order.get("driver_id") != user_id:
        await _Reply(client, user_id).send("❌ Заказ не найден.", keyboard=back_to_main_rows())
        return
    pid = order.get("passenger_id")
    if not pid:
        await _Reply(client, user_id).send("❌ Пассажир не найден.", keyboard=back_to_main_rows())
        return
    start_relay_session(user_id, pid, oid, hours=2.0)
    await _Reply(client, user_id).send(
        f"🔗 Релей‑соединение с пассажиром заказа #{oid} "
        "(20 минут). Пишите анонимно.",
        keyboard=back_to_main_rows())


# ==================== ПОМОЩЬ ====================
async def driver_help(client, user_id, update, ctx):
    """Справка по работе бота."""
    await _answer_cb(client, update)
    ver = S.get_driver_verification_status(user_id)
    extra = ""
    if ver == "approved" and not balance_hidden():
        bl = _bl(user_id)
        access_h = _access_hours(user_id)
        if bl:
            extra += f"\n💰 Баланс: {bl}\n"
        extra += f"🕒 Доступ: {access_h:.1f} ч\n"
    help_text = (
        "ℹ️ <b>Справка: ТаксиМакс для водителей</b>\n\n"
        "• <b>Главное меню</b> — заказы, статус, гео, профиль.\n"
        "• <b>Статус работы</b> — выход на линию/снятие/перерыв.\n"
        "• <b>Текущие заказы</b> — управление активным заказом.\n"
        "• <b>Список заказов</b> — список для принятия.\n"
        "• <b>Обновить гео</b> — отправить текущую геопозицию.\n"
        "• <b>Мой профиль</b> — документы, статистика, история, помощь.\n"
    )
    if not balance_hidden():
        help_text += "• <b>Баланс</b> — пополнение и покупка часов.\n"
    help_text += (
        "\n💡 Чтобы получать заказы — выйдите на линию!"
    )
    await _send_menu(client, user_id, help_text + extra, back_to_main_rows(), replace_previous=1)


# ==================== РЕЛЕЙНЫЙ ЧАТ ====================
async def driver_relay_message(client, user_id, update, ctx):
    """Перехват сообщений — релей‑пересылка от любого участника в один конец."""
    fsm = get_context(user_id)
    state = await fsm.get_state()
    if state != STATE_RELAY:
        return
    sess = get_active_relay_for_user(user_id)
    if not sess:
        return
    msg = update.message.text if update.message else ""
    text = sanitize_input(msg)
    if not text:
        return
    try:
        partner_id = int(sess.get("partner_id"))
    except (TypeError, ValueError):
        return
    await client.send_message(user_id=partner_id, text=text)


# ==================== РЕГИСТРАЦИЯ РОУТЕРА ====================
def register_driver(router: Router) -> None:
    """Регистрация всех callback/message/state‑хендлеров водителя."""

    # --- Старт ---
    router.set_role_menu("driver", driver_start)

    # --- Главное меню ---
    router.on_callback("driver:menu")(driver_menu)

    # --- Регистрация ---
    router.on_callback("driver:next_car_model")(driver_next_car_model)
    router.on_callback("driver:confirm_username")(driver_confirm_username)
    router.on_callback("driver:start_registration")(driver_start_registration)
    router.on_state(STATE_REG_NAME)(reg_name)
    router.on_state(STATE_REG_CAR_MODEL)(reg_car_model)
    router.on_state(STATE_REG_CAR_NUMBER)(reg_car_number)
    router.on_state(STATE_REG_CAR_COLOR)(reg_car_color)

    # --- Статус работы ---
    router.on_callback("driver:work_status")(driver_work_status)
    router.on_callback("driver:online")(driver_set_status)
    router.on_callback("driver:offline")(driver_set_status)
    router.on_callback("driver:break")(driver_set_status)

    # --- Модерация ---
    router.on_callback("driver:wait_admin")(driver_wait_admin)
    router.on_callback("driver:chat_queue")(driver_chat_queue)
    router.on_callback("driver:resubmit_moderation")(driver_resubmit_moderation)

    # --- Геолокация ---
    router.on_callback("driver:location")(driver_request_location)
    router.on_state(STATE_AWAIT_LOCATION)(driver_location_input)
    router.on_state(STATE_AWAIT_FINAL_GEO)(driver_trip_final_geo)

    # --- Профиль ---
    router.on_callback("driver:profile")(driver_profile)
    router.on_callback("driver:profile:edit_name")(driver_edit_name)
    router.on_callback("driver:profile:edit_phone")(driver_edit_phone)
    router.on_callback("driver:profile:edit_car")(driver_edit_car_menu)
    router.on_callback("driver:car:edit_model")(driver_edit_car_model)
    router.on_callback("driver:car:edit_number")(driver_edit_car_number)
    router.on_callback("driver:car:edit_color")(driver_edit_car_color)
    router.on_state(STATE_EDIT_NAME)(process_edit_name)
    router.on_state(STATE_EDIT_PHONE)(process_edit_phone)
    router.on_state(STATE_EDIT_CAR_MODEL)(process_edit_car_model)
    router.on_state(STATE_EDIT_CAR_NUMBER)(process_edit_car_number)
    router.on_state(STATE_EDIT_CAR_COLOR)(process_edit_car_color)

    # --- Документы ---
    router.on_callback("driver:docs")(driver_docs)
    router.on_callback("driver:docs:selfie")(driver_doc_upload_selfie)
    router.on_callback("driver:docs:car:left")(driver_doc_upload_car_left)
    router.on_callback("driver:docs:car:right")(driver_doc_upload_car_right)
    router.on_callback("driver:docs:car:front")(driver_doc_upload_car_front)
    router.on_callback("driver:docs:car:back")(driver_doc_upload_car_back)
    router.on_callback("driver:docs:car:interior")(driver_doc_upload_car_interior)
    router.on_state(STATE_UPLOAD_SELFIE)(driver_doc_received)
    router.on_state(STATE_UPLOAD_CAR_LEFT)(driver_doc_received)
    router.on_state(STATE_UPLOAD_CAR_RIGHT)(driver_doc_received)
    router.on_state(STATE_UPLOAD_CAR_FRONT)(driver_doc_received)
    router.on_state(STATE_UPLOAD_CAR_BACK)(driver_doc_received)
    router.on_state(STATE_UPLOAD_CAR_INTERIOR)(driver_doc_received)

    # --- Баланс ---
    router.on_callback("driver:balance")(driver_balance)
    router.on_callback("driver:balance:topup")(driver_balance_topup)
    router.on_state(STATE_BALANCE_TOPUP)(process_balance_topup)
    router.on_callback("driver:balance:buy:")(driver_balance_buy_access)
    router.on_callback("driver:balance:history")(driver_balance_history)

    # --- Статистика ---
    router.on_callback("driver:stats")(driver_stats)
    router.on_callback("driver:stats:day")(driver_stats_day)
    router.on_callback("driver:stats:week")(driver_stats_week)
    router.on_callback("driver:stats:month")(driver_stats_month)
    router.on_callback("driver:stats:general")(driver_stats_general)
    router.on_callback("driver:stats:custom")(driver_stats_custom)
    router.on_state(STATE_STATS_CUSTOM)(process_stats_custom)

    # --- Заказы ---
    router.on_callback("driver:orders_available")(driver_orders_available)
    router.on_callback("driver:select_order:")(driver_select_order)
    router.on_callback("order:accept:")(driver_accept_order)
    router.on_callback("driver:current_order")(driver_current_order)
    router.on_callback("driver:start_driving:")(driver_start_driving)
    router.on_callback("driver:yandex_nav:")(driver_yandex_nav)
    router.on_callback("driver:report_waiting:")(driver_report_waiting)
    router.on_callback("driver:wait_start:")(driver_wait_start)
    router.on_callback("driver:wait_stop:")(driver_wait_stop)
    router.on_callback("driver:complete_trip:")(driver_complete_trip)
    router.on_callback("driver:cancel_order:")(driver_cancel_order)
    router.on_callback("driver:cancel_order")(driver_cancel_order)
    router.on_callback("driver:contact:")(driver_contact_passenger)
    router.on_callback("driver:contact")(driver_contact_passenger)
    router.on_callback("driver:arrived:")(driver_arrived)

    # --- История ---
    router.on_callback("driver:orders_history")(driver_orders_history)
    router.on_callback("driver:history_detail:")(driver_history_contact)

    # --- Помощь ---
    router.on_callback("driver:help")(driver_help)

    # --- Релей‑чат ---
    router.on_state(STATE_RELAY)(driver_relay_message)














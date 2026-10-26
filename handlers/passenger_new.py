"""
Пассажирские хендлеры: меню, заказ такси (FSM), мои поездки, профиль, статистика.

Сценарий заказа (FSM):
  passenger:register_name  -> ввод имени (при первом запуске)
  passenger:register_phone -> ввод телефона (при первом запуске)
  passenger:await_pickup   -> ввод адреса подачи (текст/гео)
  passenger:await_dest     -> ввод адреса назначения (текст/гео)
  passenger:await_offer_price -> ввод своей цены
  -> расчёт цены -> order_actions_menu -> confirm_order -> создание заказа (pending)
  -> order_management_menu (отмена / редактирование / своя цена / отзыв)
"""
from __future__ import annotations

import logging
import re

from config import settings
from datetime import datetime
from core import storage
from core.driver_support import (
    remove_order_from_driver_queue, stop_waiting, cancel_waiting_ticker,
    get_active_relay_for_user, get_relay_partner_id, start_relay_session,
    STATE_RELAY,
)
from core.fsm import get_context
from core import geocoder
from core.tariff_manager import (
    calculate_trip_price,
)
from core.service_area import is_city_allowed, get_allowed_preview
from core.router import Router
from max_api.keyboards import (
    callback_button, request_contact_button, request_geo_location_button,
)
from ui.reply import Reply, sanitize_input

logger = logging.getLogger(__name__)
S = storage.storage


def _menu(client, user_id):
    """Экран меню: переписывает сообщение, на котором нажали кнопку."""
    return Reply(client, user_id, edit_last=True, track_menu=True)


def _ask(client, user_id):
    """Запрос адреса, гео или текста: отдельное сообщение, меню не затирает."""
    return Reply(client, user_id, edit_last=False, track_menu=False)


def _normalize_phone(phone: str) -> str:
    """Приводит номер к виду 7XXXXXXXXXX (или оставляет как есть, если странный)."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return phone.strip()


async def notify_drivers_about_order(client, order_id: int) -> None:
    """Уведомить всех онлайн-водителей о новом/обновлённом заказе.

    Вынесена на уровень модуля — вызывается из ``register_passenger``
    (confirm_order), background-задач main.py и напрямую из тестов.
    """
    order = S.get_order(order_id)
    if not order:
        return
    drivers = S.get_online_drivers()
    already_notified = set(order.get("notified_driver_ids", []) or [])
    text = (
        f"🔔 <b>Новый заказ #{order_id}</b>\n\n"
        f"📍 {order.get('pickup', '')}\n"
        f"🎯 {order.get('destination', '')}\n"
        f"📏 {order.get('distance', 0):.1f} км\n"
        f"💰 {order.get('price', 0)} руб."
    )
    kb = [[callback_button("✅ Принять заказ", f"order:accept:{order_id}")]]
    for d in drivers:
        did = d.get("user_id")
        if not did:
            continue
        # Не дублируем уведомление тому же водителю.
        if did in already_notified:
            continue
        S.add_notified_driver(order_id, did)
        try:
            await Reply(client, did).send(text, keyboard=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось уведомить водителя %s: %s", did, exc)


def register_passenger(router: Router) -> None:
    # ==================== ЗАПОЛНЕНИЕ ПРОФИЛЯ (имя + телефон) ====================
    async def ask_profile(client, user_id, update, ctx):
        fsm = get_context(user_id)
        await fsm.set_state("passenger:register_name")
        await _ask(client, user_id).send(
            "✍️ <b>Заполнение профиля</b>\n\n"
            "Введите ваше имя (например: Иван):",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:register")(ask_profile)

    async def register_name(client, user_id, update, ctx):
        fsm = get_context(user_id)
        name = sanitize_input(update.message.text) if update.message else ""
        if not name:
            await _ask(client, user_id).send("Имя не может быть пустым. Попробуйте снова.",
                                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        S.update_user(user_id, name=sanitize_input(name))
        await fsm.set_state("passenger:register_phone")
        await _ask(client, user_id).send(
            f"✅ Имя сохранено: <b>{sanitize_input(name)}</b>\n\n"
            "Теперь отправьте номер телефона кнопкой ниже:",
            keyboard=[[request_contact_button("📱 Отправить мой номер")],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_state("passenger:register_name")(register_name)

    async def register_phone(client, user_id, update, ctx):
        fsm = get_context(user_id)
        message = update.message
        contact = message.contact if message else None
        phone = ""
        if contact and contact.phone:
            phone = str(contact.phone)
        elif message and message.text:
            # Пользователь мог ввести номер вручную
            candidate = (message.text or "").strip()
            if re.fullmatch(r"[\d\s\-\+\(\)]{6,20}", candidate):
                phone = candidate
        phone = _normalize_phone(phone)
        if not phone:
            await _ask(client, user_id).send("Номер телефона не передан. Отправьте его кнопкой.",
                                             keyboard=[[request_contact_button("📱 Отправить мой номер")]])
            return
        S.update_user(user_id, phone=_normalize_phone(sanitize_input(phone)))
        await fsm.clear()
        await _menu(client, user_id).send(
            "✅ Профиль заполнен!\n\n"
            f"👤 Имя: {S.get_user(user_id).get('name') or '-'}\n"
            f"📱 Телефон: {phone}",
            keyboard=[[callback_button("🚕 Создать поездку", "passenger:new_order")],
                      [callback_button("📋 Мои заказы", "passenger:active_orders")],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_state("passenger:register_phone")(register_phone)

    # ==================== ГЛАВНОЕ МЕНЮ ПАССАЖИРА ====================
    async def passenger_menu(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        user = S.get_user(user_id) or {}
        # Если нет имени/телефона — предлагаем заполнить
        if not user.get("name") or not user.get("phone"):
            await ask_profile(client, user_id, update, ctx)
            return
        active = S.get_passenger_active_order(user_id)
        rows = [
            [callback_button("🚕 Создать поездку", "passenger:new_order")],
            [callback_button("📋 Мои заказы", "passenger:active_orders")],
            [callback_button("📜 История заказов", "passenger:trips")],
            [callback_button("👤 Профиль", "passenger:profile")],
            [callback_button("📊 Статистика", "passenger:stats")],
        ]
        if active:
            rows.insert(0, [callback_button(
                f"🚖 Активный заказ #{active['id']}",
                f"passenger:order:{active['id']}")])
        if not S.has_role(user_id, storage.ROLE_DRIVER):
            rows.append([callback_button("🚗 Стать водителем", "passenger:become_driver")])
        roles = S.compute_roles(user_id)
        if len(roles) > 1:
            rows.append([callback_button("🔄 Сменить роль", "menu:roles")])
        welcome = S.admin_data.get("passenger_welcome",
                                   "🚗 Меню пассажира. Выберите действие:")
        await reply.send(welcome, keyboard=rows)

    router.set_role_menu(storage.ROLE_PASSENGER, passenger_menu)
    router.on_callback("passenger:menu")(passenger_menu)

    # ==================== ПРОФИЛЬ ====================
    async def passenger_profile(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        user = S.get_user(user_id) or {}
        phone = user.get("phone") or "-"
        await reply.send(
            f"👤 <b>Профиль</b>\n\n"
            f"Имя: {user.get('name') or '-'}\n"
            f"Телефон: {phone}\n"
            f"Рейтинг: {user.get('rating', 5.0)}\n"
            f"Верификация: {user.get('driver_verification_status') or '-'}\n"
            f"Статус: {user.get('status', 'offline')}",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]]
        )

    router.on_callback("passenger:profile")(passenger_profile)

    # ==================== СОЗДАНИЕ ЗАКАЗА ====================
    async def new_order(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        if S.get_passenger_active_order(user_id):
            await reply.send("❌ У вас уже есть активный заказ. "
                             "Завершите или отмените его перед новым.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        fsm = get_context(user_id)
        await fsm.clear()
        await fsm.set_state("passenger:await_pickup")
        await fsm.update_data(stage="pickup")
        await reply.send(
            "📍 <b>Адрес подачи</b>\n\n"
            "Откуда вы хотите заказать такси?\n\n"
            "📝 Например: <i>г. Абакан, ул. Ленина 1</i>\n"
            "📍 Или отправьте геолокацию:",
            keyboard=[[request_geo_location_button("📍 Отправить геолокацию", quick=True)],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:new_order")(new_order)

    # --- ввод адреса подачи ---
    async def await_pickup(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        location = update.message.location if update.message else None
        address = sanitize_input(update.message.text) if update.message else ""
        if location:
            lat, lon = location.latitude, location.longitude
            if lat is None or lon is None:
                await reply.send("❌ Не удалось определить координаты. "
                                 "Отправьте геолокацию ещё раз.",
                                 keyboard=[[request_geo_location_button("📍 Геолокация", quick=True)],
                                           [callback_button("↪️ Меню", "passenger:menu")]])
                return
            addr, city = await geocoder.get_address_by_coordinates(lat, lon)
            if not addr:
                addr = f"{lat:.5f}, {lon:.5f}"
        elif address:
            coords, city = await geocoder.geocode_address(address)
            addr = address
            if not coords:
                await reply.send("❌ Не удалось определить адрес подачи. "
                                 "Попробуйте указать город, например: <i>г. Абакан, ул. Ленина 1</i>\n\n"
                                 "Или отправьте геолокацию.",
                                 keyboard=[[request_geo_location_button("📍 Геолокация", quick=True)],
                                           [callback_button("↪️ Меню", "passenger:menu")]])
                return
            lat, lon = coords["lat"], coords["lon"]
        else:
            await reply.send("Пожалуйста, отправьте адрес текстом или геолокацию.")
            return

        if not is_city_allowed(city):
            allowed = get_allowed_preview()
            await reply.send(
                f"⚠️ Адреса вне зоны обслуживания.\n\n"
                f"🏙️ Город: {city}\n\n"
                f"Разрешённые города: {', '.join(allowed)}",
                keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return

        await fsm.update_data(pickup=addr, pickup_city=city,
                              pickup_coords={"lat": lat, "lon": lon})
        await fsm.update_data(stage="destination")
        await fsm.set_state("passenger:await_dest")
        await reply.send(
            f"✅ Адрес подачи сохранён: <b>{addr}</b>\n\n"
            "Теперь укажите, КУДА нужно доехать (адрес назначения):\n\n"
            "📝 Например: <i>г. Абакан, пр. Мира 25</i>\n"
            "📍 Или отправьте геолокацию:",
            keyboard=[[request_geo_location_button("📍 Отправить геолокацию", quick=True)],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_state("passenger:await_pickup")(await_pickup)

    # --- ввод адреса назначения ---
    async def await_dest(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        data = await fsm.get_data()
        location = update.message.location if update.message else None
        address = sanitize_input(update.message.text) if update.message else ""
        if location:
            lat, lon = location.latitude, location.longitude
            if lat is None or lon is None:
                await reply.send("❌ Не удалось определить координаты. "
                                 "Отправьте геолокацию ещё раз.",
                                 keyboard=[[request_geo_location_button("📍 Геолокация", quick=True)],
                                           [callback_button("↪️ Меню", "passenger:menu")]])
                return
            addr, city = await geocoder.get_address_by_coordinates(lat, lon)
            if not addr:
                addr = f"{lat:.5f}, {lon:.5f}"
        elif address:
            pickup_city = data.get("pickup_city", "")
            destination_with_city = f"{address}, {pickup_city}" if pickup_city else address
            coords, city = await geocoder.geocode_address(destination_with_city)
            if not coords:
                coords, city = await geocoder.geocode_address(address)
            addr = address
            if not coords:
                await reply.send("❌ Не удалось определить адрес назначения. "
                                 "Попробуйте указать город, например: <i>г. Абакан, ул. Ленина 1</i>\n\n"
                                 "Или отправьте геолокацию.",
                                 keyboard=[[request_geo_location_button("📍 Геолокация", quick=True)],
                                           [callback_button("↪️ Меню", "passenger:menu")]])
                return
            lat, lon = coords["lat"], coords["lon"]
        else:
            await reply.send("Пожалуйста, отправьте адрес текстом или геолокацию.")
            return

        if not is_city_allowed(city):
            allowed = get_allowed_preview()
            await reply.send(
                f"⚠️ Адреса вне зоны обслуживания.\n\n"
                f"🏙️ Город: {city}\n\n"
                f"Разрешённые города: {', '.join(allowed)}",
                keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return

        wps = data.get("waypoints", [])
        wp_coords = [(float(w["lat"]), float(w["lon"])) for w in wps
                     if w.get("lat") is not None and w.get("lon") is not None]
        distance, duration = await geocoder.calculate_distance_and_time(
            data.get("pickup_coords", {}).get("lat"),
            data.get("pickup_coords", {}).get("lon"), lat, lon,
            waypoints=wp_coords)
        if distance is None:
            await reply.send("❌ Не удалось построить маршрут. "
                             "Проверьте адреса и попробуйте ещё раз.",
                             keyboard=[[callback_button("📍 Остановить заказ", "passenger:menu")]])
            return

        price = calculate_trip_price(distance, duration,
                                     pickup_city=data.get("pickup_city"), destination_city=city)
        await fsm.update_data(destination=addr, destination_city=city,
                              destination_coords={"lat": lat, "lon": lon},
                              distance=distance, time_minutes=duration, price=price)
        await fsm.update_data(stage="confirm")
        await fsm.set_state(None)  # экран подтверждения управляется кнопками
        await reply.send(
            "📋 <b>Подтверждение заказа</b>\n\n"
            f"📍 Подача: {data['pickup']}\n"
            f"🎯 Назначение: {addr}\n"
            f"📏 Расстояние: {distance:.1f} км\n"
            f"⏱️ Время в пути: ~{duration} мин\n\n"
            f"💰 <b>Стоимость: {price} руб.</b>",
            keyboard=[[callback_button("👍 Подтвердить заказ", "passenger:confirm_order")],
                      [callback_button("💬 Предложить свою цену", "passenger:offer_own_price")],
                      [callback_button("✏️ Редактировать адреса", "passenger:edit_addresses")],
                      [callback_button("📍 Остановка по пути", "passenger:waypoint")],
                      [callback_button("⭐ Избранное", "passenger:favorites")]])

    router.on_state("passenger:await_dest")(await_dest)

    # --- подтверждение заказа ---
    async def confirm_order(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        fsm = get_context(user_id)
        data = await fsm.get_data()
        if not data.get("pickup") or not data.get("destination"):
            await reply.send("❌ Данные заказа утеряны. Начните создание заново: /start",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            await fsm.clear()
            return

        order_id = S.create_order({
            "passenger_id": user_id,
            "pickup": data["pickup"],
            "destination": data["destination"],
            "pickup_coords": data.get("pickup_coords"),
            "destination_coords": data.get("destination_coords"),
            "distance": data.get("distance", 0),
            "time_minutes": data.get("time_minutes", 0),
            "price": data.get("price", 0),
            "pickup_city": data.get("pickup_city"),
            "destination_city": data.get("destination_city"),
            "waypoints": data.get("waypoints", []),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        })
        await fsm.clear()
        await reply.send(
            f"🚖 <b>Заказ #{order_id} создан!</b>\n\n"
            "Ищем водителя... Вы получите уведомление, когда водитель примет заказ.\n\n"
            f"📍 {data['pickup']} → {data['destination']}\n"
            f"💰 {data.get('price', 0)} руб.",
            keyboard=[[callback_button(f"🚖 Статус заказа #{order_id}",
                                      f"passenger:order:{order_id}")]])
        await notify_drivers_about_order(client, order_id)

    router.on_callback("passenger:confirm_order")(confirm_order)

    # ==================== МЕНЮ УПРАВЛЕНИЯ ЗАКАЗОМ ====================
    async def order_management(client, user_id, update, ctx):
        order_id = int(update.callback.payload.split(":")[-1])
        order = S.get_order(order_id)
        if not order or order.get("passenger_id") != user_id:
            await _menu(client, user_id).send("❌ Заказ не найден.",
                                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        status_names = {
            "pending": "🔎 Ищем водителя",
            "accepted": "🚗 Водитель назначен",
            "driver_arrived": "📍 Водитель на месте",
            "in_progress": "🚖 В пути",
            "completed": "✅ Завершён",
            "cancelled": "❌ Отменён",
        }
        driver_name = "—"
        reply = _menu(client, user_id)
        car = "—"
        if order.get("driver_id"):
            d = S.get_user(order["driver_id"]) or {}
            driver_name = f"{d.get('driver_name') or d.get('name') or '—'}"
            car = f"{d.get('car_model') or ''} {d.get('car_number') or ''} ({d.get('car_color') or ''})"
        text = (
            f"🚖 <b>Заказ #{order_id}</b>\n\n"
            f"📋 Статус: {status_names.get(order.get('status', 'pending'), order.get('status'))}\n\n"
            f"📍 {order['pickup']}\n"
            f"🎯 {order['destination']}\n\n"
            f"💰 Стоимость: {order.get('price', 0)} руб.\n\n"
            f"👤 Водитель: {driver_name}\n"
            f"🚗 {car}"
        )
        rows = [
            [callback_button("❌ Отменить заказ", f"passenger:cancel:{order_id}")],
            [callback_button("💬 Предложить свою цену", f"passenger:offer_price:{order_id}")],
            [callback_button("🚨 Жалоба на авто", "passenger:mismatch")],
            [callback_button("↪️ Меню", "passenger:menu")],
        ]
        await reply.send(text, keyboard=rows)

    router.on_callback("passenger:order_management:")(order_management)

    # ==================== СТАТУС / АКТИВНЫЕ ЗАКАЗЫ ====================
    async def active_orders(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        orders = S.get_orders_by_passenger(user_id)
        active = [o for o in orders if o.get("status") in ("pending", "accepted",
                                                           "driver_arrived", "in_progress")]
        if not active:
            await reply.send("📋 <b>Активные заказы</b>\n\n"
                             "У вас нет активных заказов.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        lines = ["📋 <b>Ваши активные заказы</b>\n\n"]
        for o in active:
            icon = {"pending": "🔎", "accepted": "🚗", "driver_arrived": "📍",
                    "in_progress": "🚖"}.get(o.get("status"), "•")
            lines.append(f"{icon} #{o['id']} — {o['pickup']} → {o['destination']} ({o.get('price',0)} руб)")
        await reply.send("\n".join(lines),
                         keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:active_orders")(active_orders)

    # ==================== ИСТОРИЯ ====================
    async def trips(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        orders = S.get_orders_by_passenger(user_id)
        if not orders:
            await reply.send("📜 <b>История заказов</b>\n\n"
                             "У вас пока нет поездок.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        status_icons = {"completed": "✅", "cancelled": "❌", "pending": "🔎",
                        "accepted": "🚗", "in_progress": "🚖", "driver_arrived": "📍"}
        lines = ["📜 <b>История заказов (последние 20)</b>\n\n"]
        for o in orders[-20:]:
            icon = status_icons.get(o.get("status", ""), "•")
            lines.append(
                f"{icon} #{o['id']} — {o['pickup']} → {o['destination']} "
                f"({o.get('price', 0)} ₽, {o.get('status', '')})"
            )
        await reply.send("\n".join(lines),
                         keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:trips")(trips)

    # ==================== СТАТИСТИКА ====================
    async def stats(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        rows = [
            [callback_button("📊 За сегодня", "passenger:stats:today"),
             callback_button("📊 За неделю", "passenger:stats:week")],
            [callback_button("📊 За месяц", "passenger:stats:month")],
            [callback_button("↪️ Меню", "passenger:menu")],
        ]
        await reply.send("📊 <b>Статистика</b>\n\nВыберите период:", keyboard=rows)

    router.on_callback("passenger:stats")(stats)

    async def stats_period(client, user_id, update, ctx):
        """Статистика пассажира за период: passenger:stats:<period>."""
        reply = _menu(client, user_id)
        period = "today"
        if update.callback:
            parts = update.callback.payload.split(":")
            if len(parts) >= 3 and parts[-1] in ("today", "week", "month", "all"):
                period = parts[-1]

        from datetime import datetime, timedelta
        now = datetime.now()
        titles = {"today": "За сегодня", "week": "За неделю",
                  "month": "За месяц", "all": "За всё время"}
        if period == "today":
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            since = now - timedelta(days=7)
        elif period == "month":
            since = now - timedelta(days=30)
        else:
            since = None

        orders = S.get_orders_by_passenger(user_id)
        completed = []
        for o in orders:
            if o.get("status") != "completed":
                continue
            if since is None:
                completed.append(o)
                continue
            raw = o.get("completed_at") or o.get("created_at")
            if not raw:
                continue
            try:
                if datetime.fromisoformat(raw) >= since:
                    completed.append(o)
            except (TypeError, ValueError):
                continue

        if not completed:
            await reply.send(f"📊 <b>{titles.get(period, period)}</b>\n\n"
                             "Пока нет завершённых поездок за этот период.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return

        total = sum(o.get("price", 0) for o in completed)
        dist = sum(o.get("distance", 0) for o in completed)
        lines = [f"📊 <b>{titles.get(period, period)}</b>\n",
                 f"🚖 Поездок: {len(completed)}",
                 f"💰 Потрачено: {total} руб.",
                 f"📏 Расстояние: {dist:.1f} км"]
        await reply.send("\n".join(lines),
                         keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:stats:")(stats_period)

    async def show_order(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        order_id = int(update.callback.payload.split(":")[-1])
        order = S.get_order(order_id)
        if not order or order.get("passenger_id") != user_id:
            await reply.send("❌ Заказ не найден.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        status_names = {
            "pending": "🔎 Ищем водителя",
            "accepted": "🚗 Водитель назначен",
            "driver_arrived": "📍 Водитель на месте",
            "in_progress": "🚖 В пути",
            "completed": "✅ Завершён",
            "cancelled": "❌ Отменён",
        }
        driver_name = "—"
        car = "—"
        if order.get("driver_id"):
            d = S.get_user(order["driver_id"]) or {}
            driver_name = f"{d.get('driver_name') or d.get('name') or '—'}"
            car = f"{d.get('car_model') or ''} {d.get('car_number') or ''} ({d.get('car_color') or ''})"
        text = (
            f"🚖 <b>Заказ #{order_id}</b>\n\n"
            f"📋 Статус: {status_names.get(order.get('status', 'pending'), order.get('status'))}\n\n"
            f"📍 {order['pickup']}\n"
            f"🎯 {order['destination']}\n\n"
            f"💰 Стоимость: {order.get('price', 0)} руб.\n\n"
        )
        if order.get("driver_id"):
            text += (
                f"👤 Водитель: {driver_name}\n"
                f"🚗 {car}\n\n"
            )
        rows = []
        if order["status"] in ("pending", "accepted", "driver_arrived"):
            rows.append([callback_button("❌ Отменить заказ", f"passenger:cancel:{order_id}")])
            rows.append([callback_button("💬 Предложить свою цену",
                                         f"passenger:offer_price:{order_id}")])
            rows.append([callback_button("🚨 Жалоба на авто", "passenger:mismatch")])
        elif order["status"] in ("in_progress", "driver_arrived"):
            rows.append([callback_button("⏳ Жду водителя", f"passenger:wait_more:{order_id}"),
                         callback_button("❌ Отмена ожидания", f"passenger:cancel_wait:{order_id}")])
        if order.get("driver_id"):
            rows.append([callback_button("💬 Написать водителю",
                                         f"passenger:contact:{order_id}")])
            rows.append([callback_button("📞 Позвонить", f"passenger:call:{order_id}")])
        rows.append([callback_button("↪️ Меню", "passenger:menu")])
        await reply.send(text, keyboard=rows)


    async def passenger_contact_order(client, user_id, update, ctx):
        await _answer_cb(client, update)
        try:
            order_id = int(update.callback.payload.split(":")[-1])
        except (AttributeError, ValueError):
            await _menu(client, user_id).send("❌ Некорректный заказ.")
            return
        order = S.get_order(order_id)
        if not order or order.get("passenger_id") != user_id:
            await _menu(client, user_id).send("❌ Заказ не найден.")
            return
        pid = order.get("driver_id")
        if not pid:
            await _menu(client, user_id).send("❌ Водитель пока не назначен.")
            return
        start_relay_session(user_id, pid, order_id, hours=24.0)
        await fsm.clear()
        await fsm.set_state(STATE_RELAY)
        await _menu(client, user_id).send(
            "🔗 Подключена связь с водителем.\n"
            "Пишите сообщения в чат; безопасный для техники.",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    async def passenger_call_order(client, user_id, update, ctx):
        await _answer_cb(client, update)
        try:
            order_id = int(update.callback.payload.split(":")[-1])
        except (AttributeError, ValueError):
            await _menu(client, user_id).send("❌ Некорректный заказ.")
            return
        order = S.get_order(order_id)
        if not order or order.get("passenger_id") != user_id:
            await _menu(client, user_id).send("❌ Заказ не найден.")
            return
        pid = order.get("driver_id")
        if not pid:
            await _menu(client, user_id).send("❌ Водитель пока не назначен.")
            return
        start_relay_session(user_id, pid, order_id, hours=24.0)
        await fsm.clear()
        await fsm.set_state(STATE_RELAY)
        await _menu(client, user_id).send(
            "📞 Позвонил(а) пассажир. Ожидает ответа в чате.",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:contact:")(passenger_contact_order)
    router.on_callback("passenger:call:")(passenger_call_order)
    router.on_callback("passenger:order:")(show_order)

    # --- своя цена для нового заказа ---
    async def offer_own_price(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        await fsm.set_state("passenger:await_offer_price")
        await reply.send(
            "💬 <b>Предложить свою цену</b>\n\n"
            "Введите вашу цену (в рублях), например: 150",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:offer_own_price")(offer_own_price)

    # --- редактирование адресов ---
    async def edit_addresses(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        fsm = get_context(user_id)
        data = await fsm.get_data()
        if not data.get("pickup"):
            await reply.send("Данные заказа утеряны. Начните заново: /start",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        rows = []
        if data.get("pickup_coords"):
            rows.append([callback_button("✏️ Изменить адрес подачи",
                                         "passenger:edit_pickup")])
        rows.append([callback_button("✏️ Изменить адрес назначения",
                                     "passenger:edit_destination")])
        rows.append([callback_button("⭐ Добавить остановку", "passenger:waypoint")])
        rows.append([callback_button("↪️ Меню", "passenger:menu")])
        await reply.send("📋 <b>Редактирование заказа</b>\n\n"
                         f"📍 {data.get('pickup', '—')}\n"
                         f"🎯 {data.get('destination', '—')}",
                         keyboard=rows)

    router.on_callback("passenger:edit_addresses")(edit_addresses)

    async def edit_pickup(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        await fsm.set_state("passenger:await_pickup")
        await fsm.update_data(stage="pickup")
        await reply.send("✏️ <b>Новый адрес подачи</b>\n\n"
                         "Отправьте новый адрес (текстом или геолокацией):",
                         keyboard=[[request_geo_location_button("📍 Геолокация", quick=True)],
                                   [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:edit_pickup")(edit_pickup)

    async def edit_destination(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        await fsm.set_state("passenger:await_dest")
        await fsm.update_data(stage="destination")
        await reply.send("✏️ <b>Новый адрес назначения</b>\n\n"
                         "Отправьте новый адрес (текстом или геолокацией):",
                         keyboard=[[request_geo_location_button("📍 Геолокация", quick=True)],
                                   [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:edit_destination")(edit_destination)

    # --- своя цена (ввод) ---
    async def await_offer_price(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        text = (update.message.text or "").strip() if update.message else ""
        try:
            price = int(float(text))
            if price <= 0:
                raise ValueError
        except ValueError:
            await reply.send("Введите число — сумму в рублях, например: 150",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        data = await fsm.get_data()
        await fsm.update_data(price=price)
        await fsm.set_state(None)  # возврат к экрану подтверждения

        # если цена предложена по уже созданному заказу — обновляем его
        order_id = data.get("offer_order_id")
        if order_id and data.get("stage") == "offer_price":
            order = S.get_order(order_id)
            if order and order.get("passenger_id") == user_id:
                S.update_order(order_id, price=price)
                await reply.send(
                    f"💰 <b>Цена обновлена: {price} руб.</b>\n\n"
                    f"Заказ #{order_id} ждёт водителя.",
                    keyboard=[[callback_button(f"🚖 Статус заказа #{order_id}",
                                               f"passenger:order:{order_id}")],
                              [callback_button("↪️ Меню", "passenger:menu")]])
                return

        await reply.send(
            f"💰 Предложенная цена: {price} руб.\n\n"
            "Проверьте адреса и подтвердите заказ:",
            keyboard=[[callback_button("👍 Подтвердить заказ", "passenger:confirm_order")],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_state("passenger:await_offer_price")(await_offer_price)

    # --- ждать водителя / отмена ожидания ---
    async def wait_more(client, user_id, update, ctx):
        order_id = int(update.callback.payload.split(":")[-1])
        order = S.get_order(order_id)
        if not order or order.get("passenger_id") != user_id:
            await _menu(client, user_id).send("❌ Заказ не найден.",
                                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        await _menu(client, user_id).send(
            "✅ Вы сообщили водителю, что готовы подождать.\n\n"
            "Если водитель опаздывает дольше обычного — "
            "вы можете отменить ожидание или заказ.",
            keyboard=[[callback_button("❌ Отменить ожидание",
                                       f"passenger:cancel_wait:{order_id}")],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:wait_more:")(wait_more)

    async def cancel_wait(client, user_id, update, ctx):
        await cancel_order(client, user_id, update, ctx)

    router.on_callback("passenger:cancel_wait:")(cancel_wait)

    # --- отмена заказа ---
    async def cancel_order(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        fsm = get_context(user_id)
        try:
            order_id = int(update.callback.payload.split(":")[-1])
        except (AttributeError, ValueError):
            await reply.send("❌ Некорректный заказ.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        order = S.get_order(order_id)
        if not order or order.get("passenger_id") != user_id:
            await reply.send("❌ Заказ не найден.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        if order.get("status") not in ("pending", "accepted", "driver_arrived"):
            await reply.send("❌ Этот заказ уже нельзя отменить.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return

        S.update_order(order_id, status="cancelled", price=order.get("price", 0))
        driver_id = order.get("driver_id")
        if driver_id:
            remove_order_from_driver_queue(driver_id, order_id)
        stop_waiting(order_id)
        cancel_waiting_ticker(order_id)
        await fsm.clear()

        # уведомляем водителя
        driver_id = order.get("driver_id")
        if driver_id:
            await Reply(client, driver_id).send(
                f"❌ Пассажир отменил заказ #{order_id}.",
                keyboard=[[callback_button("🚗 Меню водителя", "driver:menu")]])

        # админ-уведомление об отмене (если назначен водитель)
        if driver_id:
            await _notify_admins_order_cancelled(client, order_id, order)

        await reply.send(
            f"✅ Заказ #{order_id} отменён.\n\n"
            f"📍 {order.get('pickup', '—')} → {order.get('destination', '—')}",
            keyboard=[[callback_button("🚕 Создать поездку", "passenger:new_order")],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:cancel:")(cancel_order)

    # --- предложить свою цену для существующего заказа ---
    async def offer_price_for_order(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        try:
            order_id = int(update.callback.payload.split(":")[-1])
        except (AttributeError, ValueError):
            await reply.send("❌ Некорректный заказ.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        order = S.get_order(order_id)
        if not order or order.get("passenger_id") != user_id:
            await reply.send("❌ Заказ не найден.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        if order.get("status") not in ("pending", "accepted", "driver_arrived"):
            await reply.send("❌ Предложить цену можно только по активному заказу.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return

        await fsm.set_state("passenger:await_offer_price")
        await fsm.update_data(stage="offer_price", offer_order_id=order_id)
        await reply.send(
            f"💬 <b>Предложить свою цену</b>\n\n"
            f"Текущая цена заказа #{order_id}: {order.get('price', 0)} руб.\n\n"
            f"Введите вашу сумму (в рублях), например: 250",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:offer_price:")(offer_price_for_order)

    async def _notify_admins_order_cancelled(client, order_id, order):
        """Уведомление админам об отмене заказа пассажиром."""
        if not S.get_admin_ids():
            return
        d = S.get_user(order.get("driver_id")) if order.get("driver_id") else {}
        driver_txt = f"{(d.get('driver_name') or d.get('name') or '—')} ({d.get('phone') or '—'})" if d else "не был назначен"
        text = (f"❌ <b>Заказ #{order_id} отменён пассажиром</b>\n\n"
                f"📍 {order.get('pickup', '—')}\n"
                f"🎯 {order.get('destination', '—')}\n"
                f"👤 Водитель: {driver_txt}")
        for admin_id in S.get_admin_ids():
            try:
                await Reply(client, admin_id).send(
                    text, keyboard=[[callback_button("📊 Админ-панель", "a:menu")]])
            except Exception:
                pass

    # --- жалоба на авто ---
    async def mismatch(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        await reply.send(
            "🚨 <b>Жалоба на автомобиль принята</b>\n\n"
            "Опишите, чем автомобиль не совпадает с ожиданиями:\n\n"
            "🚗 Марка/модель: не та\n"
            "🔢 Госномер не совпадает\n"
            "🎨 Цвет не совпадает\n"
            "👤 Водитель не похож на фото",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:mismatch")(mismatch)

    # --- остановка / избранное ---
    async def waypoint(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        await reply.send(
            "⏳ <b>Добавить остановку</b>\n\n"
            "Отправьте адрес остановки, через который нужно проехать:",
            keyboard=[[request_geo_location_button("📍 Геолокация", quick=True)],
                      [callback_button("↪️ Меню", "passenger:menu")]])
        await fsm.set_state("passenger:await_waypoint")

    router.on_callback("passenger:waypoint")(waypoint)

    async def await_waypoint(client, user_id, update, ctx):
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        location = update.message.location if update.message else None
        address = sanitize_input(update.message.text) if update.message else ""
        if location:
            lat, lon = location.latitude, location.longitude
            if lat is None or lon is None:
                await reply.send("❌ Не удалось определить координаты.")
                return
            addr, city = await geocoder.get_address_by_coordinates(lat, lon)
            if not addr:
                addr = f"{lat:.5f}, {lon:.5f}"
        elif address:
            coords, city = await geocoder.geocode_address(address)
            if not coords:
                await reply.send("❌ Не удалось определить адрес остановки. " "Попробуйте ещё раз.")
                return
            lat, lon = coords["lat"], coords["lon"]
            addr = address
        else:
            await reply.send("Пожалуйста, отправьте адрес текстом или геолокацию.")
            return
        data = await fsm.get_data()
        waypoints = data.get("waypoints", [])
        waypoints.append({"text": addr, "lat": lat, "lon": lon})
        await fsm.update_data(waypoints=waypoints)
        await fsm.set_state(None)
        # Пересчитываем цену с учётом остановок
        pickup_coords = data.get("pickup_coords", {})
        dest_coords = data.get("destination_coords", {})
        if pickup_coords and dest_coords:
            wp_coords = [(float(w["lat"]), float(w["lon"])) for w in waypoints
                         if w.get("lat") is not None and w.get("lon") is not None]
            dist, dur = await geocoder.calculate_distance_and_time(
                pickup_coords.get("lat"), pickup_coords.get("lon"),
                dest_coords.get("lat"), dest_coords.get("lon"),
                waypoints=wp_coords)
            if dist is not None:
                price = calculate_trip_price(dist, dur,
                                             pickup_city=data.get("pickup_city"),
                                             destination_city=data.get("destination_city"))
                await fsm.update_data(distance=dist, time_minutes=dur, price=price)
                await reply.send(
                    f"✅ Остановка добавлена: {addr}\n"
                    f"📏 Новое расстояние: {dist:.1f} км, время: ~{dur} мин\n"
                    f"💰 Новая стоимость: {price} руб.",
                    keyboard=[[callback_button("✏️ Редактировать адреса",
                                               "passenger:edit_addresses")],
                              [callback_button("↪️ Меню", "passenger:menu")]])
                return
        await reply.send(
            f"✅ Остановка добавлена: {addr}. Всего остановок: {len(waypoints)}",
            keyboard=[[callback_button("✏️ Редактировать адреса",
                                       "passenger:edit_addresses")],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_state("passenger:await_waypoint")(await_waypoint)

    async def favorites(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        favs = (S.get_user(user_id) or {}).get("favorites") or []
        if not favs:
            await reply.send("❌ У вас пока нет избранных адресов.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        lines = ["⭐ <b>Избранные адреса</b>\n"]
        rows = []
        for i, f in enumerate(favs):
            oid = f.get("order_id")
            label = f.get("label") or (f"Заказ #{oid}" if oid else f"Избранное #{i+1}")
            lines.append(f"{i+1}. {label}")
            if oid:
                rows.append([callback_button(f"🚖 Использовать: {label}",
                                             f"passenger:favorite_use:{i}")])
        rows.append([callback_button("↪️ Меню", "passenger:menu")])
        await reply.send("\n".join(lines), keyboard=rows)

    router.on_callback("passenger:favorites")(favorites)

    # --- добавить заказ в избранное ---
    async def favorite_add(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        try:
            order_id = int(update.callback.payload.split(":")[-1])
        except (AttributeError, ValueError):
            await reply.send("❌ Ошибка.", keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        order = S.get_order(order_id)
        if not order or order.get("passenger_id") != user_id:
            await reply.send("❌ Заказ не найден.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        label = f"{order.get('pickup', '')} → {order.get('destination', '')}"
        S.add_favorite(user_id, order_id, label=label)
        await reply.send(
            f"⭐ Заказ #{order_id} добавлен в избранное.",
            keyboard=[[callback_button("⭐ Избранное", "passenger:favorites")],
                      [callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:favorite_add:")(favorite_add)

    # --- создать заказ из избранного ---
    async def favorite_use(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        try:
            idx = int(update.callback.payload.split(":")[-1])
        except (AttributeError, ValueError):
            await reply.send("❌ Ошибка.", keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        favs = (S.get_user(user_id) or {}).get("favorites") or []
        if idx < 0 or idx >= len(favs):
            await reply.send("❌ Избранное не найдено.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        f = favs[idx]
        oid = f.get("order_id")
        if not oid:
            await reply.send("❌ В этом избранном нет заказа.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        order = S.get_order(int(oid))
        if not order:
            await reply.send("❌ Заказ не найден.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        new_oid = S.create_order({
            "passenger_id": user_id,
            "pickup": order.get("pickup", ""),
            "destination": order.get("destination", ""),
            "pickup_coords": order.get("pickup_coords"),
            "destination_coords": order.get("destination_coords"),
            "distance": order.get("distance", 0),
            "time_minutes": order.get("time_minutes", 0),
            "price": order.get("price", 0),
            "pickup_city": order.get("pickup_city"),
            "destination_city": order.get("destination_city"),
            "waypoints": order.get("waypoints", []),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        })
        await reply.send(
            f"🚖 <b>Заказ #{new_oid} создан из избранного!</b>\n\n"
            f"📍 {order.get('pickup', '')} → {order.get('destination', '')}\n"
            f"💰 {order.get('price', 0)} руб.",
            keyboard=[[callback_button(f"🚖 Статус заказа #{new_oid}",
                                       f"passenger:order:{new_oid}")],
                      [callback_button("↪️ Меню", "passenger:menu")]])
        await notify_drivers_about_order(client, new_oid)

    router.on_callback("passenger:favorite_use:")(favorite_use)

    # --- оценить водителя после поездки ---
    async def rate_driver(client, user_id, update, ctx):
        try:
            parts = update.callback.payload.split(":")
            oid = int(parts[2])
            rating = int(parts[3])
        except (AttributeError, ValueError, IndexError):
            await reply.send("❌ Ошибка оценки.",
                             keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        if S.set_order_rating(oid, rating, passenger_id=user_id):
            await _menu(client, user_id).send(
                f"⭐ Спасибо за оценку {rating}/5 по поездке #{oid}!",
                keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
        else:
            await _menu(client, user_id).send(
                "❌ Не удалось сохранить оценку.",
                keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:rate:")(rate_driver)

    # ==================== ПОДДЕРЖКА ====================
    async def support(client, user_id, update, ctx):
        reply = _menu(client, user_id)
        contact = S.admin_data.get("support_contact", settings.support_contact)
        await reply.send(
            f"📞 <b>Поддержка</b>\n\nСвяжитесь с нами: {contact}\n\n"
            "Ожидайте ответа в рабочее время.",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])

    router.on_callback("passenger:support")(support)


    # ==================== СТАТЬ ВОДИТЕЛЕМ (анкета на модерацию) ====================
    async def become_driver(client, user_id, update, ctx):
        """Шаг 0: проверяем статус и запускаем анкету."""
        reply = _menu(client, user_id)
        status = S.get_driver_verification_status(user_id)
        if status == "pending":
            await reply.send(
                "⏳ <b>Ваша заявка уже на проверке у администратора.</b>\n\n"
                "Ожидайте решения — мы пришлём уведомление.",
                keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
            return
        if status == "approved":
            await reply.send(
                "✅ Вы уже водитель. Переключите роль через «Сменить роль».",
                keyboard=[[callback_button("🔄 Сменить роль", "menu:roles")]])
            return
        fsm = get_context(user_id)
        await fsm.clear()
        note = ""
        if status == "rejected":
            reason = S.get_reject_reason(user_id)
            note = (f"\n⚠️ <b>Прошлая заявка была отклонена.</b>\n"
                    f"Причина: {sanitize_input(reason) if reason else 'не указана'}\n")
        await fsm.set_state("passenger:driver_car_number")
        await fsm.update_data(driver_form={})
        await _ask(client, user_id).send(
            "🚗 <b>Анкета водителя</b>"
            f"{note}\n\n"
            "<b>Шаг 1 из 3.</b> Введите госномер автомобиля\n"
            "в формате <code>А123ВС 19</code>:",
            keyboard=[[callback_button("↪️ Отмена", "passenger:menu")]])

    router.on_callback("passenger:become_driver")(become_driver)

    async def driver_form_number(client, user_id, update, ctx):
        """Шаг 1: госномер авто."""
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        data = await fsm.get_data()
        form = dict(data.get("driver_form") or {})
        number = sanitize_input(update.message.text).upper() if update.message else ""
        if len(number.replace(" ", "")) < 5:
            await reply.send(
                "❌ Похоже, номер введён не полностью.\n\n"
                "Введите госномер в формате <code>А123ВС 19</code>:",
                keyboard=[[callback_button("↪️ Отмена", "passenger:menu")]])
            return
        form["car_number"] = number
        await fsm.update_data(driver_form=form)
        await fsm.set_state("passenger:driver_car_color")
        await reply.send(
            f"✅ Номер: <b>{number}</b>\n\n"
            "<b>Шаг 2 из 3.</b> Укажите цвет и марку автомобиля\n"
            "(например: <i>белый Hyundai Solaris</i>):",
            keyboard=[[callback_button("↩️ Назад", "passenger:become_driver")]])

    router.on_state("passenger:driver_car_number")(driver_form_number)

    async def driver_form_color(client, user_id, update, ctx):
        """Шаг 2: цвет/марка авто."""
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        data = await fsm.get_data()
        form = dict(data.get("driver_form") or {})
        color = sanitize_input(update.message.text) if update.message else ""
        if len(color) < 3:
            await reply.send(
                "❌ Слишком коротко. Укажите цвет и марку автомобиля\n"
                "(например: <i>белый Hyundai Solaris</i>):",
                keyboard=[[callback_button("↩️ Назад", "passenger:become_driver")]])
            return
        form["car_color"] = color
        await fsm.update_data(driver_form=form)
        await fsm.set_state("passenger:driver_car_photo")
        await reply.send(
            f"✅ Авто: <b>{color}</b>\n\n"
            "<b>Шаг 3 из 3.</b> Пришлите <b>фото автомобиля</b> одним сообщением.\n\n"
            "Требования к фото:\n"
            "• виден весь автомобиль и госномер;\n"
            "• хорошее освещение, без размытия;\n"
            "• фото без чужой личной информации.",
            keyboard=[[callback_button("↩️ Назад", "passenger:become_driver")]])

    router.on_state("passenger:driver_car_color")(driver_form_color)

    async def driver_form_photo(client, user_id, update, ctx):
        """Шаг 3: фото авто -> экран подтверждения."""
        reply = _ask(client, user_id)
        fsm = get_context(user_id)
        data = await fsm.get_data()
        form = dict(data.get("driver_form") or {})
        image = update.message.image if update.message else None
        token = image.image_token if image else None
        if not token:
            await reply.send(
                "❌ Это не фото. Пришлите <b>фото автомобиля</b> изображением.",
                keyboard=[[callback_button("↩️ Назад", "passenger:become_driver")]])
            return
        form["car_photo_token"] = token
        await fsm.update_data(driver_form=form)
        await fsm.set_state(None)
        await reply.send(
            "✅ Фото получено. Проверьте анкету:\n\n"
            f"🔢 Номер: <b>{sanitize_input(form.get('car_number'))}</b>\n"
            f"🎨 Авто: <b>{sanitize_input(form.get('car_color'))}</b>\n"
            "📷 Фото: прикреплено\n\n"
            "Нажмите «📨 Отправить на проверку», чтобы передать заявку администратору.",
            image_token=token,
            keyboard=[
                [callback_button("📨 Отправить на проверку", "passenger:driver_submit")],
                [callback_button("↩️ Заполнить заново", "passenger:become_driver")],
            ])

    router.on_state("passenger:driver_car_photo")(driver_form_photo)

    async def driver_form_submit(client, user_id, update, ctx):
        """Финальная отправка анкеты на модерацию."""
        reply = _menu(client, user_id)
        fsm = get_context(user_id)
        data = await fsm.get_data()
        form = data.get("driver_form") or {}
        number = form.get("car_number") or ""
        color = form.get("car_color") or ""
        token = form.get("car_photo_token")
        if not (number and color and token):
            await reply.send(
                "❌ Анкета заполнена не полностью. Начните заново.",
                keyboard=[[callback_button("🚗 Заполнить анкету", "passenger:become_driver")]])
            await fsm.clear()
            return
        S.submit_driver_application(
            user_id, car_number=number, car_color=color, car_photo_token=token)
        await fsm.clear()
        await reply.send(
            "📨 <b>Заявка отправлена на модерацию!</b>\n\n"
            "Администратор проверит анкету и фото. Результат придёт сюда.\n"
            "Обычно проверка занимает до 24 часов.",
            keyboard=[[callback_button("↪️ Меню", "passenger:menu")]])
        user = S.get_user(user_id) or {}
        await _notify_admins_new_driver(
            client, user_id, user.get("name", ""), number=number,
            car_color=color, photo_token=token)

    router.on_callback("passenger:driver_submit")(driver_form_submit)


    # ==================== ВСПОМОГАТЕЛЬНЫЕ ====================
    async def _notify_admins_new_driver(client, user_id, name, number="",
                                    car_color="", photo_token=None):
        """Уведомление админам: анкета водителя с фото и кнопками решения."""
        user = S.get_user(user_id) or {}
        phone = user.get("phone") or "—"
        text = (
            "🚗 <b>Новая заявка на водителя!</b>\n\n"
            f"👤 {sanitize_input(name or 'Без имени')}\n"
            f"🆔 {user_id}\n"
            f"📱 {sanitize_input(phone)}\n"
            f"🔢 Номер: <b>{sanitize_input(number or '—')}</b>\n"
            f"🎨 Авто: <b>{sanitize_input(car_color or '—')}</b>\n"
            f"📷 Фото авто: {'прикреплено ниже' if photo_token else 'не прикреплено'}\n\n"
            "Проверьте анкету и примите решение:"
        )
        rows = [
            [callback_button("✅ Одобрить", f"a:drv:approve:{user_id}"),
             callback_button("❌ Отклонить", f"a:drv:reject:{user_id}")],
            [callback_button("🛠 Открыть модерацию", "a:moderation")],
        ]
        for admin_id in S.get_admin_ids():
            try:
                await Reply(client, admin_id).send(
                    text, keyboard=rows, image_token=photo_token)
            except Exception as exc:
                logger.warning("Не удалось уведомить админа %s: %s", admin_id, exc)


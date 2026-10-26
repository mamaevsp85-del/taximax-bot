"""Админ-панель MAX-бота «ТаксиМакс» — полный перенос из Telegram admin_bot_clean.py.

Реализует всё дерево меню администратора (адаптировано под MAX Bot API):
  Управление водителями / заказами / пассажирами, тарифы, статистика,
  менеджеры, настройки системы (антиблокировки), редактирование контента,
  массовая рассылка, модерация водителей.

Payload'ы колбэков используют префикс «a:». Роутер MAX сопоставляет
префиксы, оканчивающиеся на ':', поэтому вся панель маршрутизируется
единым диспетчером `dispatch`.
"""
from __future__ import annotations

import logging
from datetime import datetime

from config import settings
from core import storage
from core.router import Router
from core.fsm import get_context
from max_api.keyboards import callback_button
from ui.reply import Reply, escape_html, remember_from_update

logger = logging.getLogger(__name__)
S = storage.storage

BACK = "↪️ Главное меню"


class _Reply(Reply):
    """Reply для админ-панели: инлайн-меню.

    Каждое новое сообщение с клавиатурой редактирует предыдущее
    (одно сообщение-меню вместо потока новых).

    Автоматически добавляет кнопку «↪️ Назад» в ``a:menu`` в конец
    любой клавиатуры, если не передан явный ``back``.
    """

    def __init__(self, client, user_id):
        super().__init__(client, user_id, edit_last=True, default_back="a:menu")


def _is_admin(user_id: int) -> bool:
    return settings.is_admin(user_id) or S.is_manager(user_id)


def _int(payload: str, idx: int, default: int = 0) -> int:
    parts = payload.split(":")
    try:
        return int(parts[idx])
    except (IndexError, ValueError):
        return default


def _generate_user_id() -> int:
    """Генерирует свободный отрицательный user_id для пользователя без Telegram ID."""
    used = set(S.users.keys())
    uid = -1
    while uid in used:
        uid -= 1
    return uid


def register_admin(router: Router) -> None:
    # ==================== ГЛАВНОЕ МЕНЮ ====================
    async def admin_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            await _Reply(client, user_id).send("⛔ Нет доступа.")
            return
        reply = _Reply(client, user_id)
        pending = S.get_pending_drivers()
        drivers = S.get_approved_drivers()
        pending_note = f" ({len(pending)} новых!)" if pending else ""
        rows = [
            [callback_button("👥 Управление водителями", "a:drivers")],
            [callback_button("📦 Управление заказами", "a:orders")],
            [callback_button("👤 Управление пассажирами", "a:passengers")],
            [callback_button("💰 Управление тарифами", "a:tariffs")],
            [callback_button("📊 Статистика ботов", "a:stats")],
            [callback_button("👨‍💼 Менеджеры", "a:managers")],
            [callback_button("🔐 Управление админами", "a:admins")],
            [callback_button("⚙️ Настройки системы", "a:syssettings")],
            [callback_button("🧮 Настройка комиссии", "a:commission")],
            [callback_button(f"✅ Модерация водителей{pending_note}", "a:moderation")],
            [callback_button("✏️ Контент и рассылка", "a:content")],
            [callback_button("🔄 Сменить роль", "menu:roles")],
        ]
        text = (
            "👋 <b>Панель администратора</b>\n\n"
            f"👮 Заявок на модерации: {len(pending)}\n"
            f"👥 Одобренных водителей: {len(drivers)}\n"
            f"📋 Всего заказов: {len(S.orders)}\n"
            f"👤 Пользователей: {len(S.users)}"
        )
        await reply.send(text, keyboard=rows)

    # ==================== МОДЕРАЦИЯ ВОДИТЕЛЕЙ ====================
    async def moderation(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        pending = S.get_pending_drivers()
        if not pending:
            await reply.send("✅ Нет водителей, ожидающих проверки.",
                             keyboard=[[callback_button(BACK, "a:menu")]])
            return
        lines = ["🧾 <b>Ожидающие проверки водители</b>\n"]
        rows = []
        for d in pending[:25]:
            uid = d.get("user_id")
            name = d.get("name") or str(uid)
            car = d.get("car_model") or "—"
            lines.append(f"• {escape_html(name)} | {escape_html(car)}")
            rows.append([callback_button(f"👤 {name} | {car}", f"a:drv:view:{uid}")])
        rows.append([callback_button(BACK, "a:menu")])
        await reply.send("\n".join(lines), keyboard=rows)

    async def view_driver_card(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        uid = _int(update.callback.payload, 3)
        u = S.get_user(uid)
        if not u:
            await reply.send("Профиль не найден.", keyboard=[[callback_button(BACK, "a:menu")]])
            return
        st = u.get("driver_verification_status", "")
        text = (
            "👤 <b>Профиль кандидата</b>\n\n"
            f"ID: {uid}\n"
            f"Имя: {escape_html(u.get('name') or '—')}\n"
            f"Телефон: {escape_html(u.get('phone') or '—')}\n"
            f"Авто: {escape_html(u.get('car_model') or '—')} | "
            f"{escape_html(u.get('car_color') or '—')} | {escape_html(u.get('car_number') or '—')}\n"
            f"Верификация: {st or '—'}"
        )
        rows = []
        if st == "pending":
            rows.append([callback_button("✅ Одобрить", f"a:drv:approve:{uid}"),
                         callback_button("❌ Отклонить", f"a:drv:reject:{uid}")])
        rows.append([callback_button("📝 На модерацию", f"a:drv:reverify:{uid}")])
        rows.append([callback_button("🛠 Управление", f"a:drv:manage:{uid}")])
        rows.append([callback_button("🗑 Удалить", f"a:drv:delconf:{uid}")])
        rows.append([callback_button("↪️ К списку", "a:moderation")])
        await reply.send(text, keyboard=rows)

    async def approve_driver(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        uid = _int(update.callback.payload, 3)
        S.approve_driver(uid)
        try:
            await Reply(client, uid).send(
                "✅ Ваша заявка одобрена! Вы стали водителем. "
                "Нажмите /start и выберите роль «🚗 Водитель».",
                keyboard=[[callback_button("🚗 Меню водителя", "role:driver")]])
        except Exception as exc:
            logger.warning("notify driver %s approve: %s", uid, exc)
        await _Reply(client, user_id).send(
            f"✅ Водитель {uid} одобрен. Доступ открыт.",
            keyboard=[[callback_button("↪️ К списку", "a:moderation"),
                       callback_button("🏠 Меню", "a:menu")]])

    async def reject_driver(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        uid = _int(update.callback.payload, 3)
        fsm = get_context(user_id)
        await fsm.set_state("admin:reject_reason")
        await fsm.update_data(reject_uid=uid)
        await _Reply(client, user_id).send(
            f"❌ Отклонение водителя {uid}\n\nУкажите причину отказа (текст сообщением):",
            keyboard=[[callback_button(BACK, "a:menu")]])

    async def reverify_driver(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        uid = _int(update.callback.payload, 3)
        S.set_verification_status(uid, "pending")
        try:
            await Reply(client, uid).send(
                "📝 Ваша анкета отправлена на повторную модерацию.\n\n"
                "Пожалуйста, дождитесь решения администратора.")
        except Exception as exc:
            logger.warning("notify driver %s reverify: %s", uid, exc)
        await _Reply(client, user_id).send(
            f"✅ Статус pending установлен для {uid}.",
            keyboard=[[callback_button("🛠 Управление", f"a:drv:manage:{uid}"),
                       callback_button("🏠 Меню", "a:menu")]])

    # ==================== УПРАВЛЕНИЕ ВОДИТЕЛЯМИ ====================
    async def drivers_list(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        drivers = S.get_all_drivers()
        if not drivers:
            await reply.send("👥 Управление водителями\n\n❌ Водители не найдены",
                             keyboard=[[callback_button(BACK, "a:menu")]])
            return
        rows = [[callback_button("— Активные —", "a:noop")]]
        active = [d for d in drivers if not d["is_blocked"]]
        blocked = [d for d in drivers if d["is_blocked"]]
        for d in active:
            emoji = "🟢" if d["status"] == "online" else "🔴"
            prio = "⭐" if d["priority_seconds"] > 0 else ""
            car = f" ({d['car_model']} {d['car_number']})" if d["car_model"] else ""
            rows.append([callback_button(f"{emoji} ✅ {prio} {d['name']}{car}"[:60],
                                         f"a:drv:manage:{d['user_id']}")])
        if blocked:
            rows.append([callback_button("— Заблокированные —", "a:noop")])
            for d in blocked:
                emoji = "🟢" if d["status"] == "online" else "🔴"
                prio = "⭐" if d["priority_seconds"] > 0 else ""
                rows.append([callback_button(f"{emoji} 🚫 {prio} {d['name']}"[:60],
                                             f"a:drv:manage:{d['user_id']}")])
        rows.append([callback_button("🔎 Поиск по номеру авто", "a:drv:search")])
        rows.append([callback_button("➕ Добавить водителя вручную", "a:drv:add")])
        rows.append([callback_button(BACK, "a:menu")])
        await reply.send("👥 <b>Управление водителями</b>\n\nВыберите водителя:", keyboard=rows)

    async def driver_add_start(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        await get_context(user_id).set_state("admin:add_driver_phone")
        await _Reply(client, user_id).send(
            "➕ <b>Добавление водителя вручную</b>\n\n"
            "Введите номер телефона:",
            keyboard=[[callback_button("❌ Отмена", "a:drivers")]])

    async def driver_manage(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        did = _int(update.callback.payload, 3)
        d = S.get_driver_by_id(did)
        if not d:
            await reply.send("❌ Водитель не найден.", keyboard=[[callback_button(BACK, "a:menu")]])
            return
        block_until = ""
        if d["is_blocked"] and d["block_until"]:
            try:
                block_until = (f"\n⏰ Блокировка до: "
                               f"{datetime.fromisoformat(d['block_until']).strftime('%d.%m.%Y %H:%M')}")
            except Exception:
                block_until = "\n⏰ Блокировка: бессрочная"
        text = (
            "👤 <b>Управление водителем</b>\n\n"
            f"📝 Имя: {escape_html(d['name'])}\n"
            f"📱 Телефон: {escape_html(d['phone'] or '—')}\n"
            f"🚗 Авто: {escape_html(d['car_model'] or '—')} {escape_html(d['car_number'] or '')}\n"
            f"📊 Статус: {d['status']}\n"
            f"✅ Верификация: {d['verification_status'] or '—'}\n"
            f"🚫 Заблокирован: {'Да' if d['is_blocked'] else 'Нет'}\n"
            f"⭐ Приоритет: {d['priority_seconds']} сек\n"
            f"📈 Заказов: {d['orders_count']}\n"
            f"💰 Заработано: {d['earnings']} ₽\n"
            f"💳 Баланс: {d['balance']:.0f} ₽{block_until}"
        )
        rows = [
            [callback_button("📝 На модерацию", f"a:drv:reverify:{did}")],
            [callback_button("🔓 Разблокировать", f"a:drv:unblock:{did}")] if d["is_blocked"]
            else [callback_button("🚫 Заблокировать", f"a:drv:block:{did}")],
            [callback_button("⭐ Убрать приоритет", f"a:drv:remprio:{did}")] if d["priority_seconds"] > 0
            else [callback_button("⭐ Установить приоритет", f"a:drv:setprio:{did}")],
            [callback_button("💳 Баланс и доступ", f"a:drv:balance:{did}")],
            [callback_button("🗑 Удалить водителя", f"a:drv:delconf:{did}")],
            [callback_button("↪️ К списку", "a:drivers")],
        ]
        await reply.send(text, keyboard=rows)

    async def driver_block_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        rows = [
            [callback_button("30 минут", f"a:drv:blockdo:{did}:30"),
             callback_button("1 час", f"a:drv:blockdo:{did}:60")],
            [callback_button("3 часа", f"a:drv:blockdo:{did}:180"),
             callback_button("6 часов", f"a:drv:blockdo:{did}:360")],
            [callback_button("12 часов", f"a:drv:blockdo:{did}:720"),
             callback_button("24 часа", f"a:drv:blockdo:{did}:1440")],
            [callback_button("Безсрочно", f"a:drv:blockdo:{did}:perm")],
            [callback_button("❌ Отмена", f"a:drv:manage:{did}")],
        ]
        await _Reply(client, user_id).send("🚫 <b>Блокировка водителя</b>\n\nВыберите время:",
                                          keyboard=rows)

    async def driver_block_do(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        parts = update.callback.payload.split(":")
        if parts[4] == "perm":
            S.block_driver(did, None); dur = "бессрочно"
        else:
            mins = int(parts[4]); S.block_driver(did, mins); dur = f"{mins} минут"
        await _Reply(client, user_id).send(
            f"✅ Водитель заблокирован {dur}.",
            keyboard=[[callback_button("🛠 Управление", f"a:drv:manage:{did}"),
                       callback_button("↪️ К списку", "a:drivers")]])

    async def driver_unblock(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        S.unblock_driver(did)
        await _Reply(client, user_id).send(
            "✅ Водитель разблокирован.",
            keyboard=[[callback_button("🛠 Управление", f"a:drv:manage:{did}"),
                       callback_button("↪️ К списку", "a:drivers")]])

    async def driver_setprio(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        fsm = get_context(user_id)
        await fsm.set_state("admin:priority")
        await fsm.update_data(prio_did=did)
        await _Reply(client, user_id).send(
            "⭐ <b>Установка приоритета</b>\n\n"
            "Введите количество секунд, на которое водитель будет получать заказы раньше других\n"
            "(например 5, 10, 30):",
            keyboard=[[callback_button("❌ Отмена", f"a:drv:manage:{did}")]])

    async def driver_remprio(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        S.remove_driver_priority(did)
        await _Reply(client, user_id).send(
            "✅ Приоритет убран.",
            keyboard=[[callback_button("🛠 Управление", f"a:drv:manage:{did}")]])

    async def driver_balance(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        did = _int(update.callback.payload, 3)
        bal = S.get_balance(did)
        has = S.has_active_access(did)
        exp = S.get_access_expires(did)
        exp_text = "♾ Безлимит" if (exp and (exp - datetime.now()).days > 365 * 20) \
            else (exp.strftime("%d.%m.%Y %H:%M") if exp else "❌ Нет доступа")
        text = (
            "💳 <b>Баланс и доступ водителя</b>\n\n"
            f"ID: {did}\n"
            f"Баланс: {bal:.2f} ₽\n"
            f"Доступ: {'✅ Активен' if has else '❌ Не активен'}\n"
            f"Истекает: {exp_text}\n\nДействия:"
        )
        rows = [
            [callback_button("➕ Пополнить баланс", f"a:drv:topup:{did}")],
            [callback_button("🟢 2ч", f"a:drv:grant:{did}:2"),
             callback_button("🟢 6ч", f"a:drv:grant:{did}:6")],
            [callback_button("🟢 8ч", f"a:drv:grant:{did}:8"),
             callback_button("🟢 12ч", f"a:drv:grant:{did}:12")],
            [callback_button("♾ Безлимит", f"a:drv:unlim:{did}"),
             callback_button("🔁 Обнулить", f"a:drv:resetacc:{did}")],
            [callback_button("🛠 Управление", f"a:drv:manage:{did}")],
        ]
        await reply.send(text, keyboard=rows)

    async def driver_grant(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        hours = int(update.callback.payload.split(":")[4])
        S.grant_access(did, hours)
        await _Reply(client, user_id).send(f"✅ Выдан доступ на {hours} ч.")
        await driver_balance(client, user_id, update, ctx)

    async def driver_unlim(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        S.grant_unlimited_access(did)
        await _Reply(client, user_id).send("✅ Выдан безлимитный доступ.")
        await driver_balance(client, user_id, update, ctx)

    async def driver_resetacc(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        S.reset_access(did)
        await _Reply(client, user_id).send("✅ Доступ сброшен.")
        await driver_balance(client, user_id, update, ctx)

    async def driver_topup(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        fsm = get_context(user_id)
        await fsm.set_state("admin:topup")
        await fsm.update_data(topup_did=did)
        await _Reply(client, user_id).send(
            "Введите сумму пополнения в рублях (например 500 или 500.00):",
            keyboard=[[callback_button("❌ Отмена", f"a:drv:balance:{did}")]])

    async def driver_search(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        await get_context(user_id).set_state("admin:search_car")
        await _Reply(client, user_id).send(
            "🔎 <b>Поиск водителя по номеру авто</b>\n\nВведите номер полностью или часть номера:",
            keyboard=[[callback_button("↪️ Назад", "a:drivers")]])

    async def driver_delconf(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        await _Reply(client, user_id).send(
            "❗ Вы уверены, что хотите удалить этого водителя? Действие необратимо.",
            keyboard=[[callback_button("✅ Да, удалить", f"a:drv:del:{did}"),
                       callback_button("↪️ Отмена", f"a:drv:manage:{did}")]])

    async def driver_del(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        did = _int(update.callback.payload, 3)
        if S.delete_user(did):
            await _Reply(client, user_id).send("✅ Водитель удалён.",
                                              keyboard=[[callback_button("↪️ К списку", "a:drivers")]])
        else:
            await _Reply(client, user_id).send("❌ Не удалось удалить водителя.",
                                              keyboard=[[callback_button(BACK, "a:menu")]])

    # ==================== УПРАВЛЕНИЕ ЗАКАЗАМИ ====================
    STATUS_ICONS = {"pending": "🟡", "accepted": "🔵", "driver_arrived": "📍",
                    "in_progress": "🚖", "completed": "🟢", "cancelled": "🔴"}
    STATUS_NAMES = {"pending": "Ожидают водителя", "accepted": "В работе",
                    "completed": "Завершённые", "cancelled": "Отменённые",
                    "all": "Все заказы"}

    async def orders_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        cities = S.get_cities_list()
        stats = S.get_city_stats()
        rows = []
        text = "📦 <b>Управление заказами</b>\n\n"
        if not cities:
            text += "❌ Нет городов с заказами.\n\n"
        else:
            text += "Выберите город:\n\n"
            for city in cities[:30]:
                c = stats.get(city, {})
                label = (f"{city} | всего {c.get('total', 0)} • "
                         f"ожид. {c.get('pending', 0)} • зав. {c.get('completed', 0)}")
                rows.append([callback_button(label[:70], f"a:ordcity:{city}")])
        if S.has_orders_without_city():
            rows.append([callback_button("🗂 Без города", "a:ordnocity")])
        rows.append([callback_button("📋 Все заказы (последние)", "a:ordall")])
        rows.append([callback_button(BACK, "a:menu")])
        await reply.send(text, keyboard=rows)

    async def orders_all(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        orders = S.get_orders(limit=25)
        if not orders:
            await reply.send("📋 Последние заказы\n\n❌ Пусто",
                             keyboard=[[callback_button("↪️ Назад", "a:orders")]])
            return
        rows = []
        for o in orders:
            icon = STATUS_ICONS.get(o.get("status"), "•")
            label = (f"{icon} #{o['id']} [{o.get('city','—')}] {o.get('status')} | "
                     f"{o.get('pickup','—')} → {o.get('destination','—')}")
            rows.append([callback_button(label[:70], f"a:ordview:{o['id']}")])
        rows.append([callback_button("↪️ Назад", "a:orders")])
        await reply.send("📋 <b>Последние заказы</b>", keyboard=rows)

    async def orders_city(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        city = update.callback.payload.split(":", 2)[2]
        stats = S.get_city_stats().get(city, {})
        text = (
            f"🏙️ <b>{escape_html(city)}</b>\n\n"
            f"Всего: {stats.get('total', 0)}\n"
            f"Ожидают водителя: {stats.get('pending', 0)}\n"
            f"В работе: {stats.get('accepted', 0)}\n"
            f"Завершены: {stats.get('completed', 0)}\n"
            f"Отменены: {stats.get('cancelled', 0)}\n\nВыберите категорию:"
        )
        rows = [
            [callback_button("🟡 Ожидают водителя", f"a:ordcityst:{city}:pending")],
            [callback_button("🔵 В работе", f"a:ordcityst:{city}:accepted")],
            [callback_button("🟢 Завершённые", f"a:ordcityst:{city}:completed")],
            [callback_button("🔴 Отменённые", f"a:ordcityst:{city}:cancelled")],
            [callback_button("📋 Все (последние)", f"a:ordcityst:{city}:all")],
            [callback_button("↪️ Назад", "a:orders")],
        ]
        await _Reply(client, user_id).send(text, keyboard=rows)

    async def orders_nocity(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        orders = S.get_orders_without_city(limit=1000)
        counts = {"pending": 0, "accepted": 0, "completed": 0, "cancelled": 0}
        for o in orders:
            if o.get("status") in counts:
                counts[o["status"]] += 1
        text = (
            "🗂 <b>Без города</b>\n\n"
            f"Всего: {len(orders)}\n"
            f"Ожидают водителя: {counts['pending']}\n"
            f"В работе: {counts['accepted']}\n"
            f"Завершены: {counts['completed']}\n"
            f"Отменены: {counts['cancelled']}\n\nВыберите категорию заказов:"
        )
        rows = [
            [callback_button("🟡 Ожидают водителя", "a:ordnocyst:pending")],
            [callback_button("🔵 В работе", "a:ordnocyst:accepted")],
            [callback_button("🟢 Завершённые", "a:ordnocyst:completed")],
            [callback_button("🔴 Отменённые", "a:ordnocyst:cancelled")],
            [callback_button("📋 Все (последние)", "a:ordnocyst:all")],
            [callback_button("↪️ Назад", "a:orders")],
        ]
        await _Reply(client, user_id).send(text, keyboard=rows)

    async def orders_city_status(client, user_id, update, ctx):
        """a:ordcityst:{city}:{status}"""
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        parts = update.callback.payload.split(":")
        city = parts[2]
        status = parts[3] if len(parts) > 3 else "all"
        orders = S.get_orders_by_city(city, status=None if status == "all" else status, limit=200)
        rows = []
        header = STATUS_NAMES.get(status, "Заказы")
        text = f"{header} — {escape_html(city)}\n\n"
        if not orders:
            text += "❌ Список пуст\n"
        for o in orders[:20]:
            icon = STATUS_ICONS.get(o.get("status"), "•")
            label = (f"{icon} #{o['id']} [{o.get('status')}] "
                     f"{o.get('pickup','—')} → {o.get('destination','—')} | {o.get('price','—')}₽")
            rows.append([callback_button(label[:70], f"a:ordview:{o['id']}")])
        rows.append([callback_button("↪️ К категориям", f"a:ordcity:{city}")])
        rows.append([callback_button("🏙️ Города", "a:orders")])
        await reply.send(text, keyboard=rows)

    async def orders_nocity_status(client, user_id, update, ctx):
        """a:ordnocyst:{status}"""
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        status = update.callback.payload.split(":")[2]
        orders = S.get_orders_without_city(status=None if status == "all" else status, limit=200)
        rows = []
        header = STATUS_NAMES.get(status, "Заказы")
        text = f"{header} — Без города\n\n"
        if not orders:
            text += "❌ Список пуст\n"
        for o in orders[:20]:
            icon = STATUS_ICONS.get(o.get("status"), "•")
            label = (f"{icon} #{o['id']} [{o.get('status')}] "
                     f"{o.get('pickup','—')} → {o.get('destination','—')} | {o.get('price','—')}₽")
            rows.append([callback_button(label[:70], f"a:ordview:{o['id']}")])
        rows.append([callback_button("↪️ К категориям", "a:ordnocity")])
        rows.append([callback_button("🏙️ Города", "a:orders")])
        await reply.send(text, keyboard=rows)

    async def order_view(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        oid = _int(update.callback.payload, 2)
        o = S.get_order(oid)
        if not o:
            await reply.send("❌ Заказ не найден.", keyboard=[[callback_button(BACK, "a:orders")]])
            return
        passenger = S.get_user(o.get("passenger_id") or 0)
        driver = S.get_user(o.get("driver_id") or 0) if o.get("driver_id") else {}
        text = (
            "🧾 <b>Детали заказа</b>\n\n"
            f"# {oid}\n"
            f"Статус: {o.get('status','—')}\n"
            f"Город: {escape_html(o.get('city') or o.get('pickup_city') or '—')}\n"
            f"Откуда: {escape_html(o.get('pickup','—'))}\n"
            f"Куда: {escape_html(o.get('destination','—'))}\n"
            f"Цена: {o.get('price','—')} ₽\n"
            f"Пассажир: {escape_html(passenger.get('name','—'))} ({o.get('passenger_id')})\n"
        )
        if driver:
            text += f"Водитель: {escape_html(driver.get('name','—'))} ({o.get('driver_id')})\n"
        rows = [
            [callback_button("👤 Пассажир", f"a:ordpass:{oid}"),
             callback_button("🚗 Водитель", f"a:orddrv:{oid}")],
            [callback_button("✅ Назначить водителя", f"a:ordassign:{oid}")],
            [callback_button("❌ Отменить заказ", f"a:ordcancel:{oid}")],
            [callback_button("✏️ Изменить статус", f"a:ordestat:{oid}"),
             callback_button("💵 Изменить цену", f"a:ordeprice:{oid}")],
            [callback_button("🏙️ Изменить город", f"a:ordecity:{oid}"),
             callback_button("↩️ Перевыставить", f"a:ordrelist:{oid}")],
            [callback_button("↪️ Назад", "a:orders")],
        ]
        await reply.send(text, keyboard=rows)

    async def order_passenger(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        oid = _int(update.callback.payload, 2)
        o = S.get_order(oid)
        p = S.get_user(o.get("passenger_id") or 0) if o else {}
        if not p:
            await _Reply(client, user_id).send("❌ Пассажир не найден.")
            return
        text = ("👤 <b>Пассажир</b>\n\n"
                f"ID: {o.get('passenger_id')}\n"
                f"Имя: {escape_html(p.get('name','—'))}\n"
                f"Телефон: {escape_html(p.get('phone','—'))}")
        await _Reply(client, user_id).send(
            text, keyboard=[[callback_button("↪️ Назад", f"a:ordview:{oid}")]])

    async def order_driver(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        oid = _int(update.callback.payload, 2)
        o = S.get_order(oid)
        d = S.get_user(o.get("driver_id") or 0) if o and o.get("driver_id") else {}
        if not d:
            await _Reply(client, user_id).send("❌ Водитель не назначен.")
            return
        text = ("🚗 <b>Водитель</b>\n\n"
                f"ID: {o.get('driver_id')}\n"
                f"Имя: {escape_html(S.get_driver_name(d.get('user_id')) or d.get('name', '—'))}\n"
                f"Телефон: {escape_html(S.get_driver_phone(d.get('user_id')) or d.get('phone', '—'))}\n"
                f"Авто: {escape_html(d.get('car_model','—'))} {escape_html(d.get('car_number','—'))}")
        await _Reply(client, user_id).send(
            text, keyboard=[[callback_button("↪️ Назад", f"a:ordview:{oid}")]])

    async def order_assign(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        oid = _int(update.callback.payload, 2)
        o = S.get_order(oid)
        if not o:
            await reply.send("❌ Заказ не найден.")
            return
        online = S.get_online_drivers()
        from core.service_area import normalize_city
        o_city = normalize_city(o.get("city") or o.get("pickup_city") or "")
        candidates = []
        for d in online:
            did = d.get("user_id")
            u = S.get_user(did)
            u_city = normalize_city(u.get("current_city") or "")
            if u_city and o_city and u_city != o_city:
                continue
            candidates.append((did, u))
        if not candidates:
            await reply.send("❌ Нет подходящих водителей онлайн.",
                             keyboard=[[callback_button("↪️ К заказу", f"a:ordview:{oid}")]])
            return
        rows = []
        for did, u in candidates[:25]:
            label = f"{u.get('name','—')} | {u.get('car_model','—')} {u.get('car_number','')}"
            rows.append([callback_button(label[:60], f"a:ordassignto:{oid}:{did}")])
        rows.append([callback_button("↪️ Назад", f"a:ordview:{oid}")])
        await reply.send("Выберите водителя:", keyboard=rows)

    async def order_assign_to(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        parts = update.callback.payload.split(":")
        oid = int(parts[2]); did = int(parts[3])
        if S.assign_driver_to_order(oid, did):
            await _Reply(client, user_id).send(
                f"✅ Водитель {did} назначен на заказ #{oid}",
                keyboard=[[callback_button("↪️ К заказу", f"a:ordview:{oid}")]])
        else:
            await _Reply(client, user_id).send(
                f"❌ Не удалось назначить водителя {did} на заказ #{oid}",
                keyboard=[[callback_button("↪️ Назад", f"a:ordview:{oid}")]])

    async def order_cancel(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        oid = _int(update.callback.payload, 2)
        ok = S.cancel_order(oid)
        msg = f"✅ Заказ #{oid} отменён" if ok else f"❌ Не удалось отменить заказ #{oid}"
        await _Reply(client, user_id).send(
            msg, keyboard=[[callback_button("↪️ К заказам", "a:orders")]])

    async def order_relist(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        oid = _int(update.callback.payload, 2)
        ok = S.update_order_status(oid, "pending")
        msg = f"↩️ Заказ #{oid} возвращён в ожидающие" if ok else f"❌ Не удалось перевыставить #{oid}"
        await _Reply(client, user_id).send(
            msg, keyboard=[[callback_button("↪️ К заказу", f"a:ordview:{oid}")]])

    async def order_edit_status(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        oid = _int(update.callback.payload, 2)
        rows = [
            [callback_button("🟡 Ожидает", f"a:ordsetst:{oid}:pending")],
            [callback_button("🔵 В работе", f"a:ordsetst:{oid}:accepted")],
            [callback_button("🟢 Завершён", f"a:ordsetst:{oid}:completed")],
            [callback_button("🔴 Отменён", f"a:ordsetst:{oid}:cancelled")],
            [callback_button("↪️ Назад", f"a:ordview:{oid}")],
        ]
        await _Reply(client, user_id).send("Выберите новый статус:", keyboard=rows)

    async def order_set_status(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        parts = update.callback.payload.split(":")
        oid = int(parts[2]); st = parts[3]
        ok = S.update_order_status(oid, st)
        msg = f"✅ Статус заказа #{oid} → {st}" if ok else f"❌ Не удалось изменить статус #{oid}"
        await _Reply(client, user_id).send(
            msg, keyboard=[[callback_button("↪️ К заказу", f"a:ordview:{oid}")]])

    async def order_edit_price(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        oid = _int(update.callback.payload, 2)
        fsm = get_context(user_id)
        await fsm.set_state("admin:order_price")
        await fsm.update_data(price_oid=oid)
        await _Reply(client, user_id).send(
            f"💵 Новая цена для заказа #{oid}:\nОтправьте число (руб)",
            keyboard=[[callback_button("↪️ К заказу", f"a:ordview:{oid}")]])

    async def order_edit_city(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        oid = _int(update.callback.payload, 2)
        fsm = get_context(user_id)
        await fsm.set_state("admin:order_city")
        await fsm.update_data(city_oid=oid)
        await _Reply(client, user_id).send(
            f"🏙️ Новый город для заказа #{oid}:\nОтправьте название",
            keyboard=[[callback_button("↪️ К заказу", f"a:ordview:{oid}")]])

    # ==================== УПРАВЛЕНИЕ ПАССАЖИРАМИ ====================
    async def passengers_list(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        passengers = S.get_all_passengers()
        if not passengers:
            await reply.send("👤 Управление пассажирами\n\n❌ Пассажиры не найдены",
                             keyboard=[[callback_button(BACK, "a:menu")]])
            return
        rows = []
        for p in passengers[:30]:
            emoji = "🚫" if p["is_blocked"] else "✅"
            phone = f" ({p['phone']})" if p["phone"] else ""
            label = f"{emoji} {p['name']}{phone}"
            rows.append([callback_button(label[:60], f"a:pass:manage:{p['user_id']}")])
        rows.append([callback_button("➕ Добавить пассажира вручную", "a:pass:add")])
        rows.append([callback_button(BACK, "a:menu")])
        await reply.send("👤 <b>Управление пассажирами</b>", keyboard=rows)

    async def passenger_manage(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        pid = _int(update.callback.payload, 3)
        p = next((x for x in S.get_all_passengers() if x["user_id"] == pid), None)
        if not p:
            await reply.send("❌ Пассажир не найден.", keyboard=[[callback_button(BACK, "a:menu")]])
            return
        block_until = ""
        if p["is_blocked"] and p["block_until"]:
            try:
                block_until = (f"\n⏰ Блокировка до: "
                               f"{datetime.fromisoformat(p['block_until']).strftime('%d.%m.%Y %H:%M')}")
            except Exception:
                block_until = "\n⏰ Блокировка: бессрочная"
        text = (
            "👤 <b>Управление пассажиром</b>\n\n"
            f"📝 Имя: {escape_html(p['name'])}\n"
            f"📱 Телефон: {escape_html(p['phone'] or '—')}\n"
            f"🚫 Заблокирован: {'Да' if p['is_blocked'] else 'Нет'}\n"
            f"📈 Заказов: {p['orders_count']} (выполнено {p['completed_orders']})\n"
            f"💰 Потрачено: {p['total_spent']} ₽{block_until}"
        )
        rows = [
            [callback_button("🔓 Разблокировать", f"a:pass:unblock:{pid}")] if p["is_blocked"]
            else [callback_button("🚫 Заблокировать", f"a:pass:block:{pid}")],
            [callback_button("🗑 Удалить", f"a:pass:delconf:{pid}")],
            [callback_button("↪️ К списку", "a:passengers")],
        ]
        await reply.send(text, keyboard=rows)

    async def passenger_block_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        pid = _int(update.callback.payload, 3)
        rows = [
            [callback_button("30 минут", f"a:pass:blockdo:{pid}:30"),
             callback_button("1 час", f"a:pass:blockdo:{pid}:60")],
            [callback_button("3 часа", f"a:pass:blockdo:{pid}:180"),
             callback_button("6 часов", f"a:pass:blockdo:{pid}:360")],
            [callback_button("12 часов", f"a:pass:blockdo:{pid}:720"),
             callback_button("24 часа", f"a:pass:blockdo:{pid}:1440")],
            [callback_button("Безсрочно", f"a:pass:blockdo:{pid}:perm")],
            [callback_button("❌ Отмена", f"a:pass:manage:{pid}")],
        ]
        await _Reply(client, user_id).send("🚫 <b>Блокировка пассажира</b>\n\nВыберите время:",
                                          keyboard=rows)

    async def passenger_block_do(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        pid = _int(update.callback.payload, 3)
        parts = update.callback.payload.split(":")
        if parts[4] == "perm":
            S.block_passenger(pid, None); dur = "бессрочно"
        else:
            mins = int(parts[4]); S.block_passenger(pid, mins); dur = f"{mins} минут"
        await _Reply(client, user_id).send(
            f"✅ Пассажир заблокирован {dur}.",
            keyboard=[[callback_button("🛠 Управление", f"a:pass:manage:{pid}"),
                       callback_button("↪️ К списку", "a:passengers")]])

    async def passenger_unblock(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        pid = _int(update.callback.payload, 3)
        S.unblock_passenger(pid)
        await _Reply(client, user_id).send(
            "✅ Пассажир разблокирован.",
            keyboard=[[callback_button("🛠 Управление", f"a:pass:manage:{pid}"),
                       callback_button("↪️ К списку", "a:passengers")]])

    async def passenger_add_start(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        await get_context(user_id).set_state("admin:add_passenger_phone")
        await _Reply(client, user_id).send(
            "➕ <b>Добавление пассажира вручную</b>\n\n"
            "Введите номер телефона:",
            keyboard=[[callback_button("❌ Отмена", "a:passengers")]])

    async def passenger_delconf(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        pid = _int(update.callback.payload, 3)
        await _Reply(client, user_id).send(
            "❗ Вы уверены, что хотите удалить этого пассажира? Действие необратимо.",
            keyboard=[[callback_button("✅ Да, удалить", f"a:pass:del:{pid}"),
                       callback_button("↪️ Отмена", f"a:pass:manage:{pid}")]])

    async def passenger_del(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        pid = _int(update.callback.payload, 3)
        if S.delete_user(pid):
            await _Reply(client, user_id).send("✅ Пассажир удалён.",
                                              keyboard=[[callback_button("↪️ К списку", "a:passengers")]])
        else:
            await _Reply(client, user_id).send("❌ Не удалось удалить пассажира.",
                                              keyboard=[[callback_button(BACK, "a:menu")]])

    # ==================== УПРАВЛЕНИЕ ТАРИФАМИ ====================
    from core import tariff_manager as TM

    TARIFF_FIELDS = {
        "base_price": "Базовая стоимость",
        "included_km": "Включенные километры",
        "price_per_km": "Цена за километр",
        "price_per_minute": "Стоимость за минуту поездки",
        "min_price": "Минимальная стоимость заказа",
        "rounding_step": "Шаг округления",
        "waiting_price_per_minute": "Цена за минуту ожидания",
        "free_waiting_minutes": "Бесплатные минуты ожидания",
    }

    def _scope_kwargs():
        scope = S.admin_data.get("tariff_edit_scope") or "intercity"
        if scope == "intercity":
            return {"intercity": True, "city": None}
        return {"intercity": False, "city": scope.split(":", 1)[1]}

    async def tariffs_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        scope = S.admin_data.get("tariff_edit_scope") or "intercity"
        scope_title = "Межгород" if scope == "intercity" else f"Город: {scope.split(':',1)[1]}"
        text = (TM.get_tariff_display_text() +
                f"\n\nОбласть: <b>{escape_html(scope_title)}</b>\nВыберите область тарифа:")
        rows = [
            [callback_button("⚙️ Настройка тарифа", "a:tariff:settings")],
            [callback_button("🛣 Межгород", "a:tariff:scope:intercity")],
            [callback_button("🏙 Абакан", "a:tariff:scope:city:абакан")],
            [callback_button("🏙 Черногорск", "a:tariff:scope:city:черногорск")],
            [callback_button("🏙 Минусинск", "a:tariff:scope:city:минусинск")],
            [callback_button(BACK, "a:menu")],
        ]
        await reply.send(text, keyboard=rows)

    async def tariff_scope(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        rest = update.callback.payload.split("a:tariff:scope:", 1)[1]
        if rest == "intercity":
            S.admin_data["tariff_edit_scope"] = "intercity"
        elif rest.startswith("city:"):
            S.admin_data["tariff_edit_scope"] = f"city:{rest.split('city:',1)[1]}"
        else:
            S.admin_data["tariff_edit_scope"] = "intercity"
        S.save()
        await tariffs_menu(client, user_id, update, ctx)

    async def tariff_settings(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        text = TM.get_tariff_display_text() + "\nВыберите параметр для изменения:"
        rows = [
            [callback_button("💵 Базовая стоимость", "a:tariff:edit:base_price")],
            [callback_button("📏 Включенные км", "a:tariff:edit:included_km")],
            [callback_button("💰 Цена за доп. км", "a:tariff:edit:price_per_km")],
            [callback_button("⏰ Стоимость за минуту", "a:tariff:edit:price_per_minute")],
            [callback_button("💳 Минимальная стоимость", "a:tariff:edit:min_price")],
            [callback_button("🔢 Настройки округления", "a:tariff:rounding")],
            [callback_button("⏳ Тарификация ожидания", "a:tariff:waiting")],
            [callback_button("↪️ Назад к тарифам", "a:tariffs")],
        ]
        await reply.send(text, keyboard=rows)

    async def tariff_edit(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        field = update.callback.payload.split(":")[3]
        if field not in TARIFF_FIELDS:
            await _Reply(client, user_id).send("❌ Параметр не найден.")
            return
        tariff = TM.get_tariff_settings(**_scope_kwargs())
        cur = tariff.get(field, "—")
        fsm = get_context(user_id)
        await fsm.set_state("admin:tariff_value")
        await fsm.update_data(tariff_field=field)
        await _Reply(client, user_id).send(
            f"✏️ <b>Редактирование: {TARIFF_FIELDS[field]}</b>\n\n"
            f"Текущее значение: {cur}\n\nОтправьте новое значение:",
            keyboard=[[callback_button("❌ Отмена", "a:tariff:settings")]])

    async def tariff_rounding(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        tariff = TM.get_tariff_settings(**_scope_kwargs())
        text = (
            "🔢 <b>Настройки округления</b>\n\n"
            f"Тип округления: {tariff.get('rounding_type', 'up')}\n"
            f"Шаг округления: {tariff.get('rounding_step', 10)} ₽\n\nВыберите параметр:"
        )
        rows = [
            [callback_button("📈 Тип округления", "a:tariff:rtype")],
            [callback_button("🔢 Шаг округления", "a:tariff:edit:rounding_step")],
            [callback_button("↪️ Назад к тарифам", "a:tariff:settings")],
        ]
        await reply.send(text, keyboard=rows)

    async def tariff_rtype(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        rows = [
            [callback_button("📈 Вверх (up)", "a:tariff:setrtype:up")],
            [callback_button("📉 Вниз (down)", "a:tariff:setrtype:down")],
            [callback_button("🔄 Обычное (normal)", "a:tariff:setrtype:normal")],
            [callback_button("↪️ Назад", "a:tariff:rounding")],
        ]
        await _Reply(client, user_id).send("📈 Выберите новый тип округления:", keyboard=rows)

    async def tariff_setrtype(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        rtype = update.callback.payload.split(":")[3]
        if TM.update_tariff_setting("rounding_type", rtype):
            await _Reply(client, user_id).send(f"✅ Тип округления изменён на: {rtype}")
        await tariff_rounding(client, user_id, update, ctx)

    async def tariff_waiting(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        tariff = TM.get_tariff_settings(**_scope_kwargs())
        text = (
            "⏳ <b>Настройка тарификации ожидания</b>\n\n"
            f"💰 Цена за минуту ожидания: {tariff.get('waiting_price_per_minute', 5)} ₽/мин\n"
            f"🆓 Бесплатные минуты: {tariff.get('free_waiting_minutes', 3)} мин\n\nВыберите параметр:"
        )
        rows = [
            [callback_button("💰 Цена за минуту ожидания", "a:tariff:edit:waiting_price_per_minute")],
            [callback_button("🆓 Бесплатные минуты", "a:tariff:edit:free_waiting_minutes")],
            [callback_button("↪️ Назад к тарифам", "a:tariff:settings")],
        ]
        await reply.send(text, keyboard=rows)

    async def tariff_test(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        price = TM.calculate_trip_price(5.5, 15)
        text = (f"🧮 <b>Тестовый расчёт</b>\n\n"
                f"📏 Расстояние: 5.5 км\n⏱️ Время: 15 мин\n💰 Стоимость: {price} ₽\n\n"
                + TM.get_tariff_display_text())
        await reply.send(text, keyboard=[[callback_button("↪️ К тарифам", "a:tariffs")]])

    # ==================== СТАТИСТИКА ====================
    async def stats_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        rows = [
            [callback_button("📊 Общая статистика", "a:stats:general")],
            [callback_button("👥 Пассажиры", "a:stats:passengers")],
            [callback_button("🚗 Водители", "a:stats:drivers")],
            [callback_button("📦 Все заказы", "a:stats:orders")],
            [callback_button("📅 За сегодня", "a:stats:day")],
            [callback_button("📆 За неделю", "a:stats:week")],
            [callback_button("🗓 За месяц", "a:stats:month")],
            [callback_button("🗓 Произвольный период", "a:stats:custom")],
            [callback_button("⚡ В реальном времени", "a:stats:realtime")],
            [callback_button("📤 Экспорт данных", "a:stats:export")],
            [callback_button(BACK, "a:menu")],
        ]
        await reply.send("📊 <b>Статистика ботов</b>\n\nВыберите раздел:", keyboard=rows)

    async def stats_general(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        st = S.get_detailed_stats()
        text = (
            "📊 <b>Общая статистика системы</b>\n\n"
            f"👥 Пользователи:\n• Пассажиры: {st['passengers_total']}\n"
            f"• Водители: {st['drivers_total']} (онлайн: {st['drivers_online']})\n\n"
            f"📈 Заказы:\n• Всего: {st['orders_total']}\n• Сегодня: {st['orders_today']}\n"
            f"• За неделю: {st['orders_week']}\n• За месяц: {st['orders_month']}\n\n"
            f"💰 Финансы:\n• Общий доход: {st['revenue_total']} ₽\n"
            f"• За сегодня: {st['revenue_today']} ₽\n• За неделю: {st['revenue_week']} ₽\n\n"
            f"📊 Средние:\n• Средний чек: {st['avg_order_price']} ₽\n"
            f"• Заказов в день: {st['avg_daily_orders']}\n"
            f"🕐 Обновлено: {st['last_updated']}"
        )
        await _Reply(client, user_id).send(text, keyboard=[[callback_button("↪️ Назад", "a:stats")]])

    async def stats_passengers(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        passengers = S.get_all_passengers()
        text = f"👥 <b>Статистика пассажиров</b>\n\nВсего зарегистрировано: {len(passengers)}\n\n"
        for i, p in enumerate(passengers[:10], 1):
            text += (f"{i}. {escape_html(p['name'])}\n"
                     f"   📱 {escape_html(p['phone'] or '—')}\n"
                     f"   📊 Заказов: {p['completed_orders']}/{p['orders_count']}\n"
                     f"   💰 Потрачено: {p['total_spent']} ₽\n\n")
        if len(passengers) > 10:
            text += f"... и ещё {len(passengers) - 10} пассажиров\n"
        text += f"🕐 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        await _Reply(client, user_id).send(text, keyboard=[[callback_button("↪️ Назад", "a:stats")]])

    async def stats_drivers(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        drivers = S.get_all_drivers()
        online = [d for d in drivers if d["status"] == "online"]
        approved = [d for d in drivers if d["verification_status"] == "approved"]
        pending = [d for d in drivers if d["verification_status"] == "pending"]
        rejected = [d for d in drivers if d["verification_status"] == "rejected"]
        text = (
            f"🚗 <b>Статистика водителей</b>\n\nВсего: {len(drivers)}\n"
            f"🟢 Онлайн: {len(online)}\n✅ Одобренных: {len(approved)}\n"
            f"⏳ На модерации: {len(pending)}\n❌ Отклоненных: {len(rejected)}\n\n"
        )
        for i, d in enumerate(drivers[:10], 1):
            emoji = "🟢" if d["status"] == "online" else "🔴"
            v = {"approved": "✅", "pending": "⏳", "rejected": "❌"}.get(d["verification_status"], "❓")
            text += (f"{i}. {escape_html(d['name'])} {emoji} {v}\n"
                     f"   🚗 {escape_html(d['car_model'] or '—')} {escape_html(d['car_number'] or '')}\n"
                     f"   📈 Заказов: {d['orders_count']} | 💰 {d['earnings']} ₽\n\n")
        if len(drivers) > 10:
            text += f"... и ещё {len(drivers) - 10} водителей\n"
        text += f"🕐 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        await _Reply(client, user_id).send(text, keyboard=[[callback_button("↪️ Назад", "a:stats")]])

    async def stats_orders(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        st = S.get_detailed_stats()
        text = (
            "📈 <b>Статистика заказов</b>\n\n"
            f"📊 Общие:\n• Всего заказов: {st['orders_total']}\n• Сегодня: {st['orders_today']}\n"
            f"• За неделю: {st['orders_week']}\n• За месяц: {st['orders_month']}\n\n"
            f"📋 Статусы:\n• Ожидают водителя: {st['orders_pending']}\n"
            f"• Приняты: {st['orders_accepted']}\n• Завершены: {st['orders_completed']}\n"
            f"• Отменены: {st['orders_cancelled']}\n\n"
            f"📊 Средние:\n• Средний чек: {st['avg_order_price']} ₽\n"
            f"• Заказов в день: {st['avg_daily_orders']}\n"
            f"🕐 Обновлено: {st['last_updated']}"
        )
        await _Reply(client, user_id).send(text, keyboard=[[callback_button("↪️ Назад", "a:stats")]])

    async def _stats_period(client, user_id, period, title):
        st = S.get_revenue_stats(period=period)
        text = (
            f"📊 <b>{title}</b>\n\n"
            f"Всего: {st['orders_total']} | Выполнено: {st['orders_completed']} | "
            f"Отменено: {st['orders_cancelled']}\n"
            f"Принято: {st['orders_accepted']} | В ожидании: {st['orders_pending']}\n"
            f"Доход: {st['revenue']} ₽"
        )
        await _Reply(client, user_id).send(text, keyboard=[[callback_button("↪️ Назад", "a:stats")]])

    async def stats_realtime(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        st = S.get_realtime_stats()
        text = (
            "⚡ <b>Статистика в реальном времени</b>\n\n"
            f"👥 Активность:\n• Пользователей онлайн (5 мин): {st['online_users']}\n"
            f"• Активных заказов: {st['active_orders']}\n\n"
            f"📋 Заказы:\n• Ожидают водителя: {st['pending_orders']}\n"
            f"• В процессе: {st['accepted_orders']}\n\n"
            f"🕐 Время: {st['timestamp']}"
        )
        await _Reply(client, user_id).send(text, keyboard=[[callback_button("↪️ Назад", "a:stats")]])

    async def stats_export(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        st = S.get_detailed_stats()
        csv = (
            "Статистика системы такси\n"
            f"Дата экспорта: {st['last_updated']}\n\n"
            f"Пользователи,{st['passengers_total']},{st['drivers_total']}\n"
            f"Заказы всего,{st['orders_total']}\n"
            f"Выполнено,{st['orders_completed']}\n"
            f"Отменены,{st['orders_cancelled']}\n"
            f"Доход (руб),{st['revenue_total']}\n"
            f"Средний чек,{st['avg_order_price']}\n"
        )
        if len(csv) > 3000:
            csv = csv[:3000] + "\n... (обрезано)"
        text = ("📤 <b>Экспорт статистики</b>\n\n"
                f"📅 Дата: {st['last_updated']}\n📋 Формат: CSV\n\n"
                f"📄 Предварительный просмотр:\n<code>{escape_html(csv)}</code>")
        await _Reply(client, user_id).send(text, keyboard=[[callback_button(BACK, "a:menu")]])

    async def stats_custom_from(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        await get_context(user_id).set_state("admin:stats_from")
        await _Reply(client, user_id).send(
            "🗓 Введите дату начала в формате ГГГГ-ММ-ДД",
            keyboard=[[callback_button("↪️ Назад", "a:stats")]])

    # ==================== МЕНЕДЖЕРЫ ====================
    async def managers_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        mgrs = S.get_managers()
        text = "👨‍💼 <b>Менеджеры</b>\n\n" + ("\n".join(str(x) for x in mgrs) if mgrs else "Пока не назначены")
        rows = [[callback_button("➕ Добавить", "a:mgr:add")]]
        for uid in mgrs[:25]:
            rows.append([callback_button(f"❌ Удалить {uid}", f"a:mgr:del:{uid}")])
        rows.append([callback_button(BACK, "a:menu")])
        await reply.send(text, keyboard=rows)

    async def manager_add(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        await get_context(user_id).set_state("admin:mgr_id")
        await _Reply(client, user_id).send(
            "Введите ID пользователя для назначения менеджером:",
            keyboard=[[callback_button(BACK, "a:menu")]])

    async def manager_del(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        uid = _int(update.callback.payload, 3)
        S.remove_manager(uid)
        await _Reply(client, user_id).send(f"✅ Менеджер {uid} удалён.")
        await managers_menu(client, user_id, update, ctx)

    # ==================== НАСТРОЙКИ СИСТЕМЫ (АНТИБЛОКИРОВКИ) ====================
    def _onoff(v):
        return "🟢 Вкл" if v else "⚪ Выкл"

    async def syssettings(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        p = S.get_behavior_policy()
        text = (
            "⚙️ <b>Настройки системы → Антиблокировки</b>\n\n"
            "Настройте политику автоблокировок по отказам и отменам.\n\n"
            "🧩 Отказы (непринятие заказов):\n"
            f"• Автоблок: {_onoff(p.get('decline_block_enabled', True))}\n"
            f"• Порог подряд отказов: {int(p.get('decline_streak_block_threshold', 3))}\n"
            f"• Минут блокировки: {int(p.get('decline_streak_block_minutes', 30))}\n\n"
            "🧩 Отмены принятых заказов:\n"
            f"• Автоблок: {_onoff(p.get('cancel_block_enabled', True))}\n"
            f"• Подряд отмен: {_onoff(p.get('cancel_streak_enabled', True))} | "
            f"Порог: {int(p.get('cancel_streak_block_threshold', 2))} | "
            f"Минут: {int(p.get('cancel_streak_block_minutes', 60))}"
        )
        rows = [
            [callback_button("🔁 Автоблок (отказы)", "a:policy:toggle:decline_block_enabled")],
            [callback_button("✏️ Порог отказов", "a:policy:edit:decline_streak_block_threshold")],
            [callback_button("✏️ Минуты (отказы)", "a:policy:edit:decline_streak_block_minutes")],
            [callback_button("🔁 Автоблок (отмены)", "a:policy:toggle:cancel_block_enabled")],
            [callback_button("🔁 Подряд отмен", "a:policy:toggle:cancel_streak_enabled")],
            [callback_button("✏️ Порог отмен", "a:policy:edit:cancel_streak_block_threshold")],
            [callback_button("✏️ Минуты (подряд)", "a:policy:edit:cancel_streak_block_minutes")],
            [callback_button(BACK, "a:menu")],
        ]
        await reply.send(text, keyboard=rows)

    async def policy_toggle(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        field = update.callback.payload.split(":")[3]
        p = S.get_behavior_policy()
        S.update_behavior_policy({field: not bool(p.get(field, True))})
        await syssettings(client, user_id, update, ctx)

    async def policy_edit(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        field = update.callback.payload.split(":")[3]
        valid = {"decline_streak_block_threshold", "decline_streak_block_minutes",
                 "cancel_streak_block_threshold", "cancel_streak_block_minutes"}
        if field not in valid:
            await _Reply(client, user_id).send("Неизвестный параметр.")
            return
        fsm = get_context(user_id)
        await fsm.set_state("admin:policy_value")
        await fsm.update_data(policy_field=field)
        await _Reply(client, user_id).send(
            "Введите новое целое значение:",
            keyboard=[[callback_button("↪️ Назад", "a:syssettings")]])

    # ==================== КОНТЕНТ И РАССЫЛКА ====================
    CONTENT_ITEMS = {
        "passenger_content": "🚕 Контент для пассажиров",
        "driver_content": "🚗 Контент для водителей",
        "passenger_welcome": "🚕 Приветствие пассажиров",
        "driver_welcome": "🚗 Приветствие водителей",
        "passenger_instructions": "📋 Инструкции для пассажиров",
        "driver_instructions": "📋 Инструкции для водителей",
        "error_messages": "❌ Сообщения об ошибках",
        "success_messages": "✅ Сообщения об успехе",
    }

    async def content_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        rows = [
            [callback_button("🚕 Для пассажиров", "a:content:edit:passenger_content")],
            [callback_button("🚗 Для водителей", "a:content:edit:driver_content")],
            [callback_button("💬 Сообщения и уведомления", "a:content:messages")],
            [callback_button("📢 Массовая рассылка", "a:content:broadcast")],
            [callback_button("👁️ Предварительный просмотр", "a:content:preview")],
            [callback_button(BACK, "a:menu")],
        ]
        await reply.send("✏️ <b>Контент и рассылка</b>\n\nВыберите раздел:", keyboard=rows)

    async def content_messages(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        rows = [
            [callback_button("🚕 Приветствие пассажиров", "a:content:edit:passenger_welcome")],
            [callback_button("🚗 Приветствие водителей", "a:content:edit:driver_welcome")],
            [callback_button("📋 Инструкции для пассажиров", "a:content:edit:passenger_instructions")],
            [callback_button("📋 Инструкции для водителей", "a:content:edit:driver_instructions")],
            [callback_button("❌ Сообщения об ошибках", "a:content:edit:error_messages")],
            [callback_button("✅ Сообщения об успехе", "a:content:edit:success_messages")],
            [callback_button("↪️ Назад", "a:content")],
        ]
        await reply.send("💬 <b>Управление сообщениями и уведомлениями</b>\n\nВыберите тип:", keyboard=rows)

    async def content_edit(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        key = update.callback.payload.split(":")[3]
        if key not in CONTENT_ITEMS:
            await _Reply(client, user_id).send("❌ Неизвестный тип контента.")
            return
        cur = S.get_content(key)
        fsm = get_context(user_id)
        await fsm.set_state("admin:content")
        await fsm.update_data(content_key=key)
        await _Reply(client, user_id).send(
            f"✏️ <b>Редактирование: {CONTENT_ITEMS[key]}</b>\n\n"
            f"Текущий текст:\n{escape_html(cur)}\n\nВведите новый текст:",
            keyboard=[[callback_button(BACK, "a:content")]])

    async def content_preview(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        text = (
            "👁️ <b>Предварительный просмотр контента</b>\n\n"
            f"🚕 Для пассажиров:\n{escape_html(S.get_content('passenger_content'))}\n\n"
            f"🚗 Для водителей:\n{escape_html(S.get_content('driver_content'))}\n\n"
            f"🚕 Приветствие пассажиров:\n{escape_html(S.get_content('passenger_welcome'))}\n\n"
            f"🚗 Приветствие водителей:\n{escape_html(S.get_content('driver_welcome'))}"
        )
        await _Reply(client, user_id).send(text[:4000],
                                          keyboard=[[callback_button(BACK, "a:content")]])

    async def content_broadcast(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        rows = [
            [callback_button("👥 Всем пассажирам", "a:bcast:passengers")],
            [callback_button("🚗 Всем водителям", "a:bcast:drivers")],
            [callback_button("🌍 Всем пользователям", "a:bcast:all")],
            [callback_button("↪️ Назад", "a:content")],
        ]
        await reply.send("📢 <b>Массовая рассылка сообщений</b>\n\nВыберите аудиторию:", keyboard=rows)

    async def broadcast_start(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        audience = update.callback.payload.split(":")[2]
        fsm = get_context(user_id)
        await fsm.set_state("admin:broadcast")
        await fsm.update_data(bcast_audience=audience)
        names = {"passengers": "пассажирам", "drivers": "водителям", "all": "всем пользователям"}
        await _Reply(client, user_id).send(
            f"📢 Массовая рассылка {names.get(audience, audience)}\n\n"
            "Введите текст сообщения для рассылки:",
            keyboard=[[callback_button("↪️ Назад", "a:content")]])
    # ==================== НАСТРОЙКА КОМИССИИ ====================
    def _commission_status(v):
        return "🟢 Включена" if v else "⚪ Выключена"

    async def commission_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        s = S.get_commission_summary()
        mode_name = "Процент с заказов" if s["mode"] == "percent" else "Платный доступ (по часам)"
        text = (
            "🧮 <b>Настройка комиссии</b>\n\n"
            f"Статус: {_commission_status(s['enabled'])}\n"
            f"Режим: {mode_name}\n"
        )
        if s["mode"] == "percent":
            text += f"Процент с заказа: {s['percent']:.0f}%\n"
        text += (
            f"Балансы скрыты у водителей: {'Да' if s['balances_disabled'] else 'Нет'}\n"
            f"Всего собрано комиссии: {s['total_commission']:.0f} ₽\n\n"
        )
        if s["mode"] == "access" and s["access_prices"]:
            text += "💰 Стоимость смен (доступа):\n"
            for hours, price in s["access_prices"].items():
                text += f"• {hours} ч — {price:.0f} ₽\n"
            text += "\n"
        if not s["enabled"]:
            text += ("ℹ️ Комиссия выключена — балансы у водителей обнулены, "
                     "платные функции (покупка смен) отключены.\n")
        rows = [
            [callback_button("🔁 Включить комиссию" if not s["enabled"] else "🔁 Выключить комиссию",
                             "a:comm:toggle")],
        ]
        if s["enabled"]:
            rows.append([callback_button(
                "📊 Режим: Процент" if s["mode"] != "percent" else "📊 Режим: Платный доступ",
                "a:comm:mode")])
            if s["mode"] == "percent":
                rows.append([callback_button("✏️ Изменить процент", "a:comm:edit:percent")])
            else:
                rows.append([callback_button("💰 Стоимость смен", "a:comm:prices")])
        rows.append([callback_button("🧹 Обнулить балансы водителей", "a:comm:resetbal")])
        rows.append([callback_button(BACK, "a:menu")])
        await reply.send(text, keyboard=rows)

    async def commission_toggle(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        if S.get_commission_summary()["enabled"]:
            res = S.disable_commission_for_all()
            await reply.send(
                "⚪ <b>Комиссия выключена</b>\n\n"
                "• Балансы водителей обнулены\n"
                "• Платный доступ снят\n"
                "• Напоминания о балансе скрыты\n\n"
                f"Затронуто водителей: {res['drivers_affected']}\n\n"
                "Теперь водители работают бесплатно.",
                keyboard=[[callback_button("🧮 К настройкам комиссии", "a:commission"),
                           callback_button(BACK, "a:menu")]])
        else:
            S.enable_commission()
            await reply.send(
                "🟢 <b>Комиссия включена</b>\n\n"
                "Водителям снова доступны баланс и покупка смен.\n"
                "Настройте процент и/или стоимость смен.",
                keyboard=[[callback_button("🧮 К настройкам комиссии", "a:commission"),
                           callback_button(BACK, "a:menu")]])

    async def commission_mode(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        cfg = S.get_commission_config()
        S.set_commission(mode="access" if cfg.get("mode") != "access" else "percent")
        await commission_menu(client, user_id, update, ctx)

    async def commission_edit(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        field = update.callback.payload.split(":")[3]
        cur = S.get_commission_summary().get("percent", 0)
        fsm = get_context(user_id)
        await fsm.set_state("admin:commission_value")
        await fsm.update_data(comm_field=field)
        await _Reply(client, user_id).send(
            f"✏️ <b>Процент комиссии с заказа</b>\n\nТекущее значение: {cur:.0f}%\n\n"
            "Введите новое значение (целое число процентов):",
            keyboard=[[callback_button("❌ Отмена", "a:commission")]])

    async def commission_resetbal(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        n = S.reset_all_driver_balances()
        await _Reply(client, user_id).send(
            f"🧹 Обнулены балансы у {n} водителей.",
            keyboard=[[callback_button("🧮 К настройкам комиссии", "a:commission"),
                       callback_button(BACK, "a:menu")]])

    async def commission_prices(client, user_id, update, ctx):
        """Стоимость смен: список с кнопками изменения/добавления."""
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        prices = S.get_access_tariffs()
        text = "💰 <b>Стоимость смен (доступа)</b>\n\n"
        for hours, price in prices.items():
            text += f"• {hours} ч — {price:.0f} ₽\n"
        text += "\nВыберите смену для изменения или добавьте новую:"
        rows = []
        for hours in prices:
            rows.append([callback_button(f"✏️ {hours} ч ({prices[hours]:.0f} ₽)",
                                         f"a:comm:setprice:{hours}")])
        rows.append([callback_button("➕ Добавить смену", "a:comm:addprice")])
        rows.append([callback_button("↩️ Удалить смену", "a:comm:delprice")])
        rows.append([callback_button("↪️ Назад", "a:commission")])
        await reply.send(text, keyboard=rows)

    async def commission_setprice(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        hours = int(update.callback.payload.split(":")[3])
        fsm = get_context(user_id)
        await fsm.set_state("admin:comm_price")
        await fsm.update_data(comm_hours=hours)
        await _Reply(client, user_id).send(
            f"✏️ Новая стоимость смены {hours} ч (в рублях):",
            keyboard=[[callback_button("❌ Отмена", "a:comm:prices")]])

    async def commission_delprice(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        prices = S.get_access_tariffs()
        rows = []
        for hours in prices:
            rows.append([callback_button(f"🗑 {hours} ч ({prices[hours]:.0f} ₽)",
                                         f"a:comm:deldo:{hours}")])
        rows.append([callback_button("↩️ Назад", "a:comm:prices")])
        await reply.send("Выберите смену для удаления:", keyboard=rows)

    async def commission_addprice(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        await get_context(user_id).set_state("admin:comm_newprice")
        await _Reply(client, user_id).send(
            "➕ <b>Новая смена</b>\n\nВведите в формате: <code>часы цена</code>\n"
            "Например: <code>10 180</code> (10 часов за 180 ₽)",
            keyboard=[[callback_button("❌ Отмена", "a:comm:prices")]])

    async def commission_deldo(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        hours = int(update.callback.payload.split(":")[3])
        S.remove_access_price(hours)
        await _Reply(client, user_id).send(f"🗑 Смена {hours} ч удалена из тарифов.")
        await commission_prices(client, user_id, update, ctx)




    # ==================== ЕДИНЫЙ ДИСПЕТЧЕР КОЛБЭКОВ ====================
    async def dispatch(client, user_id, update, ctx):
        if not _is_admin(user_id):
            await _Reply(client, user_id).send("⛔ Нет доступа.")
            return
        payload = update.callback.payload
        remember_from_update(user_id, update)  # инлайн-меню: mid сообщения с кнопкой
        try:
            if payload == "a:menu":
                await admin_menu(client, user_id, update, ctx)
            elif payload == "a:noop":
                pass
            elif payload == "a:moderation":
                await moderation(client, user_id, update, ctx)
            elif payload.startswith("a:drv:"):
                await _dispatch_driver(client, user_id, update)
            elif payload == "a:drivers":
                await drivers_list(client, user_id, update, ctx)
            elif payload == "a:orders":
                await orders_menu(client, user_id, update, ctx)
            elif payload == "a:ordall":
                await orders_all(client, user_id, update, ctx)
            elif payload == "a:ordnocity":
                await orders_nocity(client, user_id, update, ctx)
            elif payload.startswith("a:ordcityst:"):
                await orders_city_status(client, user_id, update, ctx)
            elif payload.startswith("a:ordnocyst:"):
                await orders_nocity_status(client, user_id, update, ctx)
            elif payload.startswith("a:ordcity:"):
                await orders_city(client, user_id, update, ctx)
            elif payload.startswith("a:ord"):
                await _dispatch_order(client, user_id, update)
            elif payload == "a:passengers":
                await passengers_list(client, user_id, update, ctx)
            elif payload.startswith("a:pass:"):
                await _dispatch_passenger(client, user_id, update)
            elif payload == "a:tariffs":
                await tariffs_menu(client, user_id, update, ctx)
            elif payload.startswith("a:tariff:"):
                await _dispatch_tariff(client, user_id, update)
            elif payload == "a:stats":
                await stats_menu(client, user_id, update, ctx)
            elif payload.startswith("a:stats:"):
                await _dispatch_stats(client, user_id, update)
            elif payload == "a:managers":
                await managers_menu(client, user_id, update, ctx)
            elif payload == "a:admins":
                await admins_menu(client, user_id, update, ctx)
            elif payload == "a:admin_add":
                await admins_add_ask(client, user_id, update, ctx)
            elif payload.startswith("a:admindel:"):
                await admins_delete(client, user_id, update, ctx)
            elif payload.startswith("a:mgr:"):
                await _dispatch_mgr(client, user_id, update)
            elif payload == "a:syssettings":
                await syssettings(client, user_id, update, ctx)
            elif payload.startswith("a:policy:"):
                await _dispatch_policy(client, user_id, update)
            elif payload == "a:commission":
                await commission_menu(client, user_id, update, ctx)
            elif payload.startswith("a:comm:"):
                await _dispatch_commission(client, user_id, update)
            elif payload == "a:content":
                await content_menu(client, user_id, update, ctx)
            elif payload.startswith("a:content:"):
                await _dispatch_content(client, user_id, update)
            elif payload.startswith("a:bcast:"):
                await broadcast_start(client, user_id, update, ctx)
            else:
                await _Reply(client, user_id).send(
                    "Неизвестная команда.", keyboard=[[callback_button(BACK, "a:menu")]])
        except Exception as exc:
            logger.exception("admin dispatch error for %r: %s", payload, exc)
            await _Reply(client, user_id).send(
                "❌ Произошла ошибка. Попробуйте снова.",
                keyboard=[[callback_button(BACK, "a:menu")]])

    async def _dispatch_driver(client, user_id, update):
        p = update.callback.payload
        routes = {
            "view": view_driver_card, "approve": approve_driver, "reject": reject_driver,
            "reverify": reverify_driver, "manage": driver_manage, "block": driver_block_menu,
            "unblock": driver_unblock, "setprio": driver_setprio, "remprio": driver_remprio,
            "balance": driver_balance, "topup": driver_topup, "search": driver_search,
            "delconf": driver_delconf, "del": driver_del, "unlim": driver_unlim,
            "resetacc": driver_resetacc, "add": driver_add_start,
        }
        parts = p.split(":")
        action = parts[2] if len(parts) > 2 else ""
        if action == "blockdo":
            await driver_block_do(client, user_id, update, {}); return
        if action == "grant":
            await driver_grant(client, user_id, update, {}); return
        fn = routes.get(action)
        if fn:
            await fn(client, user_id, update, {})

    async def _dispatch_order(client, user_id, update):
        parts = update.callback.payload.split(":")
        routes = {
            "ordview": order_view, "ordpass": order_passenger, "orddrv": order_driver,
            "ordassign": order_assign, "ordcancel": order_cancel, "ordrelist": order_relist,
            "ordestat": order_edit_status, "ordeprice": order_edit_price,
            "ordecity": order_edit_city, "ordassignto": order_assign_to, "ordsetst": order_set_status,
        }
        fn = routes.get(parts[1])
        if fn:
            await fn(client, user_id, update, {})

    async def _dispatch_passenger(client, user_id, update):
        parts = update.callback.payload.split(":")
        action = parts[2] if len(parts) > 2 else ""
        if action == "blockdo":
            await passenger_block_do(client, user_id, update, {}); return
        routes = {"manage": passenger_manage, "block": passenger_block_menu, "unblock": passenger_unblock,
                  "add": passenger_add_start, "delconf": passenger_delconf, "del": passenger_del}
        fn = routes.get(action)
        if fn:
            await fn(client, user_id, update, {})

    async def _dispatch_tariff(client, user_id, update):
        p = update.callback.payload
        routes = {
            "a:tariff:settings": tariff_settings, "a:tariff:rounding": tariff_rounding,
            "a:tariff:rtype": tariff_rtype, "a:tariff:waiting": tariff_waiting,
            "a:tariff:test": tariff_test,
        }
        if p in routes:
            await routes[p](client, user_id, update, {}); return
        if p.startswith("a:tariff:scope:"):
            await tariff_scope(client, user_id, update, {}); return
        if p.startswith("a:tariff:setrtype:"):
            await tariff_setrtype(client, user_id, update, {}); return
        if p.startswith("a:tariff:edit:"):
            await tariff_edit(client, user_id, update, {})

    async def _dispatch_stats(client, user_id, update):
        p = update.callback.payload
        routes = {
            "a:stats:general": stats_general, "a:stats:passengers": stats_passengers,
            "a:stats:drivers": stats_drivers, "a:stats:orders": stats_orders,
            "a:stats:realtime": stats_realtime, "a:stats:export": stats_export,
            "a:stats:custom": stats_custom_from,
        }
        if p in routes:
            await routes[p](client, user_id, update, {}); return
        if p == "a:stats:day":
            await _stats_period(client, user_id, "day", "За сегодня"); return
        if p == "a:stats:week":
            await _stats_period(client, user_id, "week", "За неделю"); return
        if p == "a:stats:month":
            await _stats_period(client, user_id, "month", "За месяц")

    async def _dispatch_mgr(client, user_id, update):
        parts = update.callback.payload.split(":")
        if parts[2] == "add":
            await manager_add(client, user_id, update, {})
        elif parts[2] == "del":
            await manager_del(client, user_id, update, {})

    async def _dispatch_policy(client, user_id, update):
        parts = update.callback.payload.split(":")
        if parts[2] == "toggle":
            await policy_toggle(client, user_id, update, {})
        elif parts[2] == "edit":
            await policy_edit(client, user_id, update, {})

    async def _dispatch_commission(client, user_id, update):
        """Маршрутизация a:comm:<action>[:<id>]."""
        parts = update.callback.payload.split(":")
        action = parts[2] if len(parts) > 2 else ""
        routes = {
            "toggle": commission_toggle, "mode": commission_mode,
            "prices": commission_prices, "setprice": commission_setprice,
            "addprice": commission_addprice, "delprice": commission_delprice,
            "deldo": commission_deldo, "resetbal": commission_resetbal,
        }
        if action == "edit":
            await commission_edit(client, user_id, update, {}); return
        fn = routes.get(action)
        if fn:
            await fn(client, user_id, update, {})

    async def _dispatch_content(client, user_id, update):
        parts = update.callback.payload.split(":")
        action = parts[2] if len(parts) > 2 else ""
        if action == "messages":
            await content_messages(client, user_id, update, {})
        elif action == "edit":
            await content_edit(client, user_id, update, {})
        elif action == "preview":
            await content_preview(client, user_id, update, {})
        elif action == "broadcast":
            await content_broadcast(client, user_id, update, {})

    # ==================== УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ ====================
    async def admins_menu(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        admins = S.get_admin_ids()
        env_admins = settings.admin_ids
        lines = ["🔐 <b>Управление администраторами</b>\n\n"]
        rows = []
        for a in admins:
            u = S.get_user(a) or {}
            name = escape_html(u.get("name") or str(a))
            phone = u.get("phone") or "—"
            env_mark = " (из .env)" if a in env_admins else ""
            lines.append(f"• {name} — 📱 {phone} (-ID {a}){env_mark}")
            if a not in env_admins:
                rows.append([callback_button(f"➖ {name}", f"a:admindel:{a}")])
        rows.append([callback_button("➕ Добавить админа", "a:admin_add")])
        rows.append([callback_button(BACK, "a:menu")])
        await reply.send("\n".join(lines), keyboard=rows)

    async def admins_add_ask(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        fsm = get_context(user_id)
        await fsm.set_state("admin:await_admin_phone")
        await _Reply(client, user_id).send(
            "➕ <b>Добавление администратора</b>\n\n"
            "Введите <b>номер телефона</b>:\n"
            "Формат: <code>+7 999 123-45-67</code> или <code>79991234567</code>",
            keyboard=[[callback_button("↩️ К админам", "a:admins")]])

    async def admins_delete(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        reply = _Reply(client, user_id)
        target = int(update.callback.payload.split(":")[2])
        if target in settings.admin_ids:
            await reply.send(
                "⛔ Админ из .env не может быть удалён.",
                keyboard=[[callback_button("↩️ К админам", "a:admins")]])
            return
        if S.remove_admin(target):
            await reply.send(f"➖ Администратор {target} удалён.",
                             keyboard=[[callback_button("↩️ К админам", "a:admins")]])
        else:
            await reply.send("❌ Администратор не найден.",
                             keyboard=[[callback_button("↩️ К админам", "a:admins")]])

    # ==================== РЕГИСТРАЦИЯ ====================
    router.set_role_menu(storage.ROLE_ADMIN, admin_menu)
    router.on_callback("a:")(dispatch)
    router.on_command("admin")(admin_menu)
    router.on_command("apanel")(admin_menu)

    # ==================== FSM: ТЕКСТОВЫЙ ВВОД ====================
    async def st_admin_phone(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        fsm = get_context(user_id)
        reply = _Reply(client, user_id)
        raw = (update.message.text or "").strip()
        if not raw:
            await reply.send("Введите номер телефона.",
                             keyboard=[[callback_button("↩️ К админам", "a:admins")]])
            return
        uid = S.find_user_by_phone(raw)
        if uid is None:
            await reply.send(
                "❌ Пользователь с таким номером не найден.",
                keyboard=[[callback_button("↩️ К админам", "a:admins")]])
            return
        if S.is_admin(uid):
            await reply.send("❌ Уже является админом.",
                             keyboard=[[callback_button("↩️ К админам", "a:admins")]])
            return
        u = S.get_user(uid) or {}
        await fsm.update_data(admin_candidate=uid)
        await reply.send(
            f"⚠️ Добавить в админы?\n\n"
            f"👤 {escape_html(u.get('name') or '—')}\n"
            f"📱 {escape_html(u.get('phone') or '—')}\n"
            f"🆔 {uid}\n\n"
            f"Введите <b>ДА</b> для подтверждения:",
            keyboard=[[callback_button("↩️ Отмена", "a:admins")]])

    async def st_admin_confirm(client, user_id, update, ctx):
        if not _is_admin(user_id):
            return
        fsm = get_context(user_id)
        reply = _Reply(client, user_id)
        data = await fsm.get_data()
        uid = data.get("admin_candidate")
        if uid is None:
            await fsm.set_state(None)
            await reply.send("❌ Операция отменена.",
                             keyboard=[[callback_button("↩️ К админам", "a:admins")]])
            return
        text = (update.message.text or "").strip().lower()
        if text in ("да", "yes", "y"):
            if S.add_admin(uid):
                u = S.get_user(uid) or {}
                await reply.send(
                    f"✅ Админ добавлен!\n\n"
                    f"👤 {escape_html(u.get('name') or '—')}\n"
                    f"🆔 {uid}",
                    keyboard=[[callback_button("↩️ К админам", "a:admins")]])
            else:
                await reply.send("❌ Уже в списке.",
                                 keyboard=[[callback_button("↩️ К админам", "a:admins")]])
        else:
            await reply.send("❌ Операция отменена.",
                             keyboard=[[callback_button("↩️ К админам", "a:admins")]])
        await fsm.set_state(None)

    router.on_state("admin:await_admin_phone")(st_admin_phone)
    router.on_state("admin:await_admin_confirm")(st_admin_confirm)

    async def st_reject_reason(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        uid = int(data.get("reject_uid"))
        reason = (update.message.text or "").strip() or "Без указания причины"
        S.reject_driver(uid, reason)
        await fsm.clear()
        try:
            await Reply(client, uid).send(
                f"❌ Ваша заявка отклонена модератором.\n\nПричина: {escape_html(reason)}\n\n"
                "Вы можете обновить данные и подать заявку снова.")
        except Exception as exc:
            logger.warning("notify driver %s reject: %s", uid, exc)
        await _Reply(client, user_id).send(
            "✅ Отказ зафиксирован и водитель уведомлён.",
            keyboard=[[callback_button("↪️ К списку", "a:moderation"),
                       callback_button(BACK, "a:menu")]])

    async def st_priority(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        did = int(data.get("prio_did"))
        try:
            secs = int((update.message.text or "").strip())
            if secs < 0:
                raise ValueError
        except ValueError:
            await _Reply(client, user_id).send("❌ Введите корректное число секунд (0 или больше)")
            return
        S.set_driver_priority(did, secs)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"✅ Приоритет установлен: {secs} секунд",
            keyboard=[[callback_button("🛠 Управление", f"a:drv:manage:{did}")]])

    async def st_topup(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        did = int(data.get("topup_did"))
        try:
            amount = float((update.message.text or "").strip().replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await _Reply(client, user_id).send("❌ Некорректная сумма. Введите положительное число.")
            return
        S.add_balance(did, amount)
        await fsm.clear()
        bal = S.get_balance(did)
        await _Reply(client, user_id).send(
            f"✅ Баланс пополнен на {amount:.2f} ₽. Текущий баланс: {bal:.2f} ₽",
            keyboard=[[callback_button("↪️ Назад", f"a:drv:balance:{did}")]])

    async def st_search_car(client, user_id, update, ctx):
        fsm = get_context(user_id)
        query = (update.message.text or "").strip().lower()
        matched = S.search_drivers_by_car(query)
        if not matched:
            await _Reply(client, user_id).send("❌ Водители с таким номером не найдены.")
            return
        rows = []
        for d in matched[:30]:
            label = f"{d['name']} | {d['car_model']} {d['car_number']}"
            rows.append([callback_button(label[:60], f"a:drv:manage:{d['user_id']}")])
        rows.append([callback_button("↪️ Назад", "a:drivers")])
        await fsm.clear()
        await _Reply(client, user_id).send(f"🔎 Найдено водителей: {len(matched)}", keyboard=rows)

    async def _fsm_order_price(client, user_id, update, fsm, data):
        try:
            price = float((update.message.text or "").strip().replace(",", "."))
        except ValueError:
            await _Reply(client, user_id).send("❌ Введите корректное число")
            return
        oid = int(data.get("price_oid"))
        S.update_order_price(oid, price)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"✅ Цена заказа #{oid} изменена на {int(price)} ₽",
            keyboard=[[callback_button("↪️ К заказу", f"a:ordview:{oid}")]])

    async def st_tariff_value(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        if "price_oid" in data:
            await _fsm_order_price(client, user_id, update, fsm, data); return
        field = data.get("tariff_field")
        try:
            value = float((update.message.text or "").strip())
            if value <= 0:
                raise ValueError
        except ValueError:
            await _Reply(client, user_id).send("❌ Значение должно быть положительным числом.")
            return
        TM.update_tariff_setting(field, value)
        await fsm.clear()
        name = TARIFF_FIELDS.get(field, field)
        await _Reply(client, user_id).send(
            f"✅ {name} обновлено!\nНовое значение: {value}",
            keyboard=[[callback_button("↪️ К параметрам", "a:tariff:settings")]])

    async def st_order_price(client, user_id, update, ctx):
        fsm = get_context(user_id)
        await _fsm_order_price(client, user_id, update, fsm, await fsm.get_data())

    async def st_order_city(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        new_city = (update.message.text or "").strip()
        oid = int(data.get("city_oid"))
        S.update_order_city(oid, new_city)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"✅ Город заказа #{oid} изменён на «{escape_html(new_city)}»",
            keyboard=[[callback_button("↪️ К заказу", f"a:ordview:{oid}")]])

    async def st_policy_value(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        field = data.get("policy_field")
        try:
            value = int((update.message.text or "").strip())
            if value < 0:
                raise ValueError
        except ValueError:
            await _Reply(client, user_id).send("❌ Введите целое неотрицательное число")
            return
        S.update_behavior_policy({field: value})
        await fsm.clear()
        await _Reply(client, user_id).send(f"✅ Параметр обновлён: {value}")
        await syssettings(client, user_id, update, ctx)

    async def st_manager_id(client, user_id, update, ctx):
        fsm = get_context(user_id)
        try:
            uid = int((update.message.text or "").strip())
        except ValueError:
            await _Reply(client, user_id).send("❌ Введите корректный числовой ID.")
            return
        S.add_manager(uid)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"✅ Пользователь {uid} назначен менеджером.",
            keyboard=[[callback_button("👨‍💼 Менеджеры", "a:managers")]])

    async def st_content(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        S.set_content(data.get("content_key"), (update.message.text or "").strip())
        await fsm.clear()
        await _Reply(client, user_id).send(
            "✅ Контент обновлён!", keyboard=[[callback_button("↪️ Назад", "a:content")]])

    async def st_stats_from(client, user_id, update, ctx):
        fsm = get_context(user_id)
        try:
            datetime.strptime((update.message.text or "").strip(), "%Y-%m-%d")
        except ValueError:
            await _Reply(client, user_id).send("❌ Неверная дата. Введите в формате ГГГГ-ММ-ДД")
            return
        await fsm.update_data(stats_from=(update.message.text or "").strip())
        await fsm.set_state("admin:stats_to")
        await _Reply(client, user_id).send("Введите дату конца в формате ГГГГ-ММ-ДД")

    async def st_stats_to(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        try:
            to_dt = datetime.strptime((update.message.text or "").strip(), "%Y-%m-%d")
            from_dt = datetime.strptime(data.get("stats_from"), "%Y-%m-%d")
        except ValueError:
            await _Reply(client, user_id).send("❌ Неверная дата. Введите в формате ГГГГ-ММ-ДД")
            return
        st = S.get_revenue_stats(period="custom", date_from=from_dt, date_to=to_dt)
        await fsm.clear()
        text = (
            "📊 <b>За период</b>\n\n"
            f"Всего: {st['orders_total']} | Выполнено: {st['orders_completed']} | "
            f"Отменено: {st['orders_cancelled']}\n"
            f"Принято: {st['orders_accepted']} | В ожидании: {st['orders_pending']}\n"
            f"Доход: {st['revenue']} ₽"
        )
        await _Reply(client, user_id).send(text, keyboard=[[callback_button("↪️ Назад", "a:stats")]])

    async def st_broadcast(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        audience = data.get("bcast_audience", "all")
        text = (update.message.text or "").strip()
        await fsm.clear()
        names = {"passengers": "пассажирам", "drivers": "водителям", "all": "всем пользователям"}
        if audience == "drivers":
            targets = [d["user_id"] for d in S.get_all_drivers() if d["verification_status"] == "approved"]
        elif audience == "passengers":
            targets = [p["user_id"] for p in S.get_all_passengers()]
        else:
            targets = list(S.users.keys())
        ok = 0
        for tid in targets:
            try:
                await Reply(client, tid).send(text); ok += 1
            except Exception as exc:
                logger.debug("broadcast to %s failed: %s", tid, exc)
        await _Reply(client, user_id).send(
            f"✅ Сообщение отправлено {names.get(audience, audience)}: доставлено {ok}/{len(targets)}.",
            keyboard=[[callback_button(BACK, "a:menu")]])

    # ---------- FSM: настройка комиссии ----------
    async def st_commission_value(client, user_id, update, ctx):
        fsm = get_context(user_id)
        try:
            value = int((update.message.text or "").strip())
            if not (0 <= value <= 100):
                raise ValueError
        except ValueError:
            await _Reply(client, user_id).send("❌ Введите целое число от 0 до 100.")
            return
        S.set_commission(percent=value)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"✅ Процент комиссии установлен: {value}%",
            keyboard=[[callback_button("🧮 К настройкам комиссии", "a:commission")]])

    async def st_comm_price(client, user_id, update, ctx):
        fsm = get_context(user_id)
        data = await fsm.get_data()
        hours = int(data.get("comm_hours"))
        try:
            price = float((update.message.text or "").strip().replace(",", "."))
            if price < 0:
                raise ValueError
        except ValueError:
            await _Reply(client, user_id).send("❌ Введите корректную цену (неотрицательное число).")
            return
        if price == 0:
            S.remove_access_price(hours)
            await fsm.clear()
            await _Reply(client, user_id).send(
                f"🗑 Смена {hours} ч удалена (цена 0).",
                keyboard=[[callback_button("↩️ К стоимости смен", "a:comm:prices")]])
            return
        S.set_access_price(hours, price)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"✅ Смена {hours} ч — {price:.0f} ₽",
            keyboard=[[callback_button("↩️ К стоимости смен", "a:comm:prices")]])

    async def st_comm_newprice(client, user_id, update, ctx):
        fsm = get_context(user_id)
        parts = (update.message.text or "").strip().replace(",", ".").split()
        if len(parts) != 2:
            await _Reply(client, user_id).send(
                "❌ Формат: <code>часы цена</code>, например <code>10 180</code>")
            return
        try:
            hours = int(parts[0])
            price = float(parts[1])
            if hours <= 0 or price <= 0:
                raise ValueError
        except ValueError:
            await _Reply(client, user_id).send(
                "❌ Числа должны быть положительными. Пример: <code>10 180</code>")
            return
        S.set_access_price(hours, price)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"➕ Добавлена смена: {hours} ч — {price:.0f} ₽",
            keyboard=[[callback_button("↩️ К стоимости смен", "a:comm:prices")]])

    def _norm_phone(phone: str) -> str:
        return "".join(ch for ch in str(phone) if ch.isdigit())

    async def st_add_driver_phone(client, user_id, update, ctx):
        fsm = get_context(user_id)
        phone = (update.message.text or "").strip()
        if not _norm_phone(phone):
            await _Reply(client, user_id).send("❌ Введите корректный номер телефона.")
            return
        uid = S.find_user_by_phone(phone)
        if uid is not None:
            u = S.get_user(uid)
            if u.get("driver_verification_status") == "approved":
                await _Reply(client, user_id).send(
                    f"⚠ Пользователь с телефоном {escape_html(phone)} уже является водителем.",
                    keyboard=[[callback_button("↪️ К списку", "a:drivers")]])
                await fsm.clear()
                return
            S.update_user(uid, phone=phone)
            S.approve_driver(uid)
            await fsm.clear()
            await _Reply(client, user_id).send(
                f"✅ Пользователь {uid} ({escape_html(u.get('name') or '')}) теперь водитель.\n"
                f"Телефон: {escape_html(phone)}",
                keyboard=[[callback_button("🛠 Управление", f"a:drv:manage:{uid}"),
                           callback_button("↪️ К списку", "a:drivers")]])
            return
        await fsm.update_data(add_driver_phone=phone)
        await fsm.set_state("admin:add_driver_name")
        await _Reply(client, user_id).send(
            f"➕ Телефон: {escape_html(phone)}\n\nВведите имя:",
            keyboard=[[callback_button("❌ Отмена", "a:drivers")]])

    async def st_add_driver_name(client, user_id, update, ctx):
        fsm = get_context(user_id)
        name = (update.message.text or "").strip()
        if not name:
            await _Reply(client, user_id).send("❌ Имя не может быть пустым.")
            return
        data = await fsm.get_data()
        phone = data.get("add_driver_phone", "")
        uid = _generate_user_id()
        S.ensure_user(uid, name=name)
        S.update_user(uid, phone=phone, driver_name=name)
        S.approve_driver(uid)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"✅ Водитель {uid} ({escape_html(name)}) успешно добавлен.\n"
            f"Телефон: {escape_html(phone)}",
            keyboard=[[callback_button("🛠 Управление", f"a:drv:manage:{uid}"),
                       callback_button("↪️ К списку", "a:drivers")]])

    # ---------- FSM: ручное добавление пассажира ----------

    async def st_add_passenger_phone(client, user_id, update, ctx):
        fsm = get_context(user_id)
        phone = (update.message.text or "").strip()
        if not _norm_phone(phone):
            await _Reply(client, user_id).send("❌ Введите корректный номер телефона.")
            return
        uid = S.find_user_by_phone(phone)
        if uid is not None:
            u = S.get_user(uid)
            if u.get("driver_verification_status") == "approved":
                await _Reply(client, user_id).send(
                    f"⚠ Пользователь с телефоном {escape_html(phone)} является водителем. Пассажиром не добавлен.",
                    keyboard=[[callback_button("↪️ К списку", "a:passengers")]])
                await fsm.clear()
                return
            await fsm.clear()
            await _Reply(client, user_id).send(
                f"✅ Пользователь {uid} ({escape_html(u.get('name') or '')}) уже добавлен как пассажир.\n"
                f"Телефон: {escape_html(phone)}",
                keyboard=[[callback_button("🛠 Управление", f"a:pass:manage:{uid}"),
                           callback_button("↪️ К списку", "a:passengers")]])
            return
        await fsm.update_data(add_passenger_phone=phone)
        await fsm.set_state("admin:add_passenger_name")
        await _Reply(client, user_id).send(
            f"➕ Телефон: {escape_html(phone)}\n\nВведите имя:",
            keyboard=[[callback_button("❌ Отмена", "a:passengers")]])

    async def st_add_passenger_name(client, user_id, update, ctx):
        fsm = get_context(user_id)
        name = (update.message.text or "").strip()
        if not name:
            await _Reply(client, user_id).send("❌ Имя не может быть пустым.")
            return
        data = await fsm.get_data()
        phone = data.get("add_passenger_phone", "")
        uid = _generate_user_id()
        S.ensure_user(uid, name=name)
        S.update_user(uid, phone=phone)
        await fsm.clear()
        await _Reply(client, user_id).send(
            f"✅ Пассажир {uid} ({escape_html(name)}) успешно добавлен.\n"
            f"Телефон: {escape_html(phone)}",
            keyboard=[[callback_button("🛠 Управление", f"a:pass:manage:{uid}"),
                       callback_button("↪️ К списку", "a:passengers")]])

    for _state, _fn in [
        ("admin:reject_reason", st_reject_reason),
        ("admin:priority", st_priority),
        ("admin:topup", st_topup),
        ("admin:search_car", st_search_car),
        ("admin:tariff_value", st_tariff_value),
        ("admin:order_price", st_order_price),
        ("admin:order_city", st_order_city),
        ("admin:policy_value", st_policy_value),
        ("admin:mgr_id", st_manager_id),
        ("admin:content", st_content),
        ("admin:stats_from", st_stats_from),
        ("admin:stats_to", st_stats_to),
        ("admin:broadcast", st_broadcast),
        ("admin:commission_value", st_commission_value),
        ("admin:comm_price", st_comm_price),
        ("admin:comm_newprice", st_comm_newprice),
        ("admin:add_driver_phone", st_add_driver_phone),
        ("admin:add_driver_name", st_add_driver_name),
        ("admin:add_passenger_phone", st_add_passenger_phone),
        ("admin:add_passenger_name", st_add_passenger_name),
    ]:
        router.on_state(_state)(_fn)

"""
Роутер: распределяет обновления по хендлерам с учётом РОЛЕЙ.

Ролевая логика (согласована с заказчиком):
  - bot_started/bot_added  -> показать выбор роли (или сразу меню, если роль одна).
  - message_callback        -> обработчик по payload (с поддержкой префиксов).
  - message_created/edited  -> команда (/start, /menu) или активное состояние FSM.

Сигнатура хендлера: async def handler(client, user_id, update, ctx)
"""
from __future__ import annotations

import logging
import asyncio  # noqa: F401 used for iscoroutine check in _open_role_menu
from typing import Awaitable, Callable, Dict

from max_api.models import Update
from max_api.client import MaxClient
from ui.reply import Reply, remember_from_update, get_menu_mid
from core import storage


logger = logging.getLogger(__name__)

Handler = Callable[[MaxClient, int, Update, dict], Awaitable[None]]


class Router:
    def __init__(self, client: MaxClient) -> None:
        self.client = client
        self.callback_handlers: Dict[str, Handler] = {}
        self.state_handlers: Dict[str, Handler] = {}
        self.role_menu: Dict[str, Handler] = {}
        self.command_handlers: Dict[str, Handler] = {}
        self.role_selector_mid: Dict[int, Optional[str]] = {}

    # --- регистрация ---
    def on_callback(self, payload: str):
        def deco(fn: Handler):
            self.callback_handlers[payload] = fn
            return fn
        return deco

    def on_state(self, state: str):
        def deco(fn: Handler):
            self.state_handlers[state] = fn
            return fn
        return deco

    def set_role_menu(self, role: str, fn: Handler):
        self.role_menu[role] = fn

    def on_command(self, command: str):
        def deco(fn: Handler):
            self.command_handlers[command] = fn
            return fn
        return deco

    # --- маршрутизация ---
    async def dispatch(self, update: Update) -> None:
        user_id = update.actor_id
        if not user_id:
            logger.warning("Апдейт без actor_id пропущен: %s", update.update_type)
            return
        try:
            if update.is_bot_started or update.is_bot_added:
                await self._handle_start(user_id, update)
                logger.info("Обработан %s для %s", update.update_type, user_id)
                return
            if update.is_message_callback and update.callback:
                await self._handle_callback(user_id, update)
                logger.info("Обработан callback %r для %s", update.callback.payload, user_id)
                return
            if update.is_message_created or update.is_message_edited:
                await self._handle_message(user_id, update)
                logger.info("Обработано сообщение для %s", user_id)
                return
            logger.info("Неизвестный тип апдейта пропущен: %s", update.update_type)
        except Exception as exc:
            logger.exception("Ошибка обработки обновления: %s", exc)
            await Reply(self.client, user_id).send(
                "❌ Произошла ошибка. Попробуйте ещё раз или напишите /start."
            )

    async def _handle_start(self, user_id: int, update: Update) -> None:
        name = update.user.name if update.user else ""
        username = update.user.username if update.user else None
        storage.storage.get_or_create_user(user_id, name=name, username=username)
        await self._show_role_selector(user_id)

    async def _show_role_selector(self, user_id: int) -> None:
        # always remove any previous role selector message to avoid duplicates
        previous_mid = self.role_selector_mid.get(int(user_id))
        if previous_mid:
            try:
                await self.client.delete_message(int(previous_mid))
            except Exception:
                pass
        self.role_selector_mid[int(user_id)] = None
        roles = storage.storage.compute_roles(user_id)
        # Только пассажир -> сразу меню пассажира
        if roles == [storage.ROLE_PASSENGER]:
            await self._open_role_menu(user_id, storage.ROLE_PASSENGER)
            return
        # Несколько ролей -> выбор
        from max_api.keyboards import callback_button
        buttons = []
        if storage.ROLE_PASSENGER in roles:
            buttons.append(callback_button("🚕 Пассажир", "role:passenger"))
        if storage.ROLE_DRIVER in roles:
            buttons.append(callback_button("🚗 Водитель", "role:driver"))
        if storage.ROLE_ADMIN in roles:
            buttons.append(callback_button("🛠 Админ", "role:admin"))
        rows = [[b] for b in buttons]
        result = await Reply(self.client, user_id, edit_last=False, track_menu=False).send(
            "👤 Выберите роль:", keyboard=rows
        )
        if result:
            mid = result.get("message_id") or (result.get("message") or {}).get("message_id")
            if mid:
                self.role_selector_mid[int(user_id)] = str(mid)

    async def _open_role_menu(self, user_id: int, role: str) -> None:
        # Удаляем сообщение выбора роли, если оно ещё на экране, и заменяем
        # его собственно меню роли в одно сообщение.
        mid = self.role_selector_mid.pop(int(user_id), None)
        if mid:
            try:
                await self.client.delete_message(mid)
            except Exception:
                logger.debug("Не удалось удалить selector mid %s: %s", mid, role)
        storage.storage.set_active_role(user_id, role)
        fn = self.role_menu.get(role)
        if fn:
            res = fn(self.client, user_id, self._dummy_update(user_id), {})
            if asyncio.iscoroutine(res):
                await res

    def _dummy_update(self, user_id: int) -> Update:
        from max_api.models import Update, User
        return Update(update_type="menu_open", timestamp=0, user=User(user_id=user_id))

    async def _handle_callback(self, user_id: int, update: Update) -> None:
        payload = update.callback.payload or ""
        fn = self.callback_handlers.get(payload)
        if fn is None:
            # Поддержка префиксов (например "order:accept:123")
            for key, handler in self.callback_handlers.items():
                if key.endswith(":") and payload.startswith(key):
                    fn = handler
                    break
        if fn is None:
            logger.warning("Хендлер не найден для payload %r (зарегистрировано: %d)",
                           payload, len(self.callback_handlers))
            await Reply(self.client, user_id).send("Неизвестная команда. /start")
            return
        remember_from_update(user_id, update)  # инлайн-меню: mid сообщения с кнопкой
        mid_before = get_menu_mid(user_id)
        logger.info("DEBUG callback user=%s payload=%s mid_before=%s", user_id, payload, mid_before)
        await fn(self.client, user_id, update, {})
        mid_after = get_menu_mid(user_id)
        if mid_after != mid_before:
            logger.info("DEBUG callback user=%s mid changed %s -> %s", user_id, mid_before, mid_after)

    async def _handle_message(self, user_id: int, update: Update) -> None:
        from core.fsm import get_context
        text = (update.message.text or "").strip() if update.message else ""
        logger.info("_handle_message user=%s text=%r has_message=%s",
                    user_id, text, update.message is not None)

        if text.startswith("/"):
            cmd = text.split()[0].lower().lstrip("/").split("@")[0]
            if cmd in self.command_handlers:
                await self.command_handlers[cmd](self.client, user_id, update, {})
                return
            if cmd == "start":
                await self._show_role_selector(user_id)
                return

        # Обработка по активному состоянию FSM
        # Если пользователь в сценарии (например, в await_pickup), обрабатываем его handler
        # И возвращаемся немедленно — роль не переключаем (fixated during FSM)
        fsm = get_context(user_id)
        state = await fsm.get_state()
        if state and state in self.state_handlers:
            logger.info("_handle_message calling state handler: %s for user=%s", state, user_id)
            await self.state_handlers[state](self.client, user_id, update, {})
            return

        # По активной роли — только если FSM состояние НЕ активно
        role = storage.storage.get_active_role(user_id)
        if role in self.role_menu:
            await self._open_role_menu(user_id, role)
            return
        await self._show_role_selector(user_id)


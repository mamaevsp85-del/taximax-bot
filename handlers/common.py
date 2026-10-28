"""
Общие хендлеры: выбор и переключение роли (анкета водителя — в passenger_new.py).

Логика ролей (согласованная с пользователем):
  - Новый пользователь видит только пассажирское меню + кнопку «🚗 Стать водителем».
  - Кнопка «стать водителем» открывает анкету: номер авто, цвет, фото авто.
  - Заявка уходит на модерацию: админ одобряет или отклоняет с причиной.
  - Админ видит все три роли: админ / пассажир / водитель.
"""
from __future__ import annotations

import logging

from core.router import Router
from ui.reply import Reply

logger = logging.getLogger(__name__)

# Адрес Mini App (для локального теста - localhost, для продакшена - внешний URL)
import os as _os
MINI_APP_URL = _os.environ.get("MINI_APP_URL", "https://taximax-bot.onrender.com")


def register_common(router: Router) -> None:
    # --- Команда /minimax - открывает Mini App ---
    async def open_minimax(client, user_id, update, ctx):
        # Мост «бот -> мини-апп»: приложение узнает пользователя по этому ID
        from core import storage as _st
        _st.storage.touch_bot_user(user_id)
        # Персональная ссылка с user_id для идентификации в Mini-App
        personal_url = f"{MINI_APP_URL}/?uid={user_id}"
        from max_api.keyboards import link_button, inline_keyboard, keyboard_attachment
        kb = keyboard_attachment([[link_button("🚀 Открыть Mini-App", personal_url)]])
        text = (
            "🎉 Добро пожаловать в Mini-app ТаксиМакс!\n\n"
            "Нажмите кнопку ниже, чтобы открыть приложение."
        )
        await Reply(client, user_id).send(text, parse_mode="HTML", attachments=kb)

    router.on_command("minimax")(open_minimax)

    # --- Переключение роли ---
    async def open_role(client, user_id, update, ctx):
        role = update.callback.payload.split(":", 1)[1]
        await router._open_role_menu(user_id, role)

    router.on_callback("role:")(open_role)

    # --- Переключение ролей (кнопка «🔄 Сменить роль») ---
    async def show_roles(client, user_id, update, ctx):
        await router._show_role_selector(user_id)

    router.on_callback("menu:roles")(show_roles)


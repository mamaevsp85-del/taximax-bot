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
MINI_APP_URL = "http://127.0.0.1:8000"


def register_common(router: Router) -> None:
    # --- Команда /minimax - открывает Mini App ---
    async def open_minimax(client, user_id, update, ctx):
        # Для локального теста показываем инструкцию
        text = (
            "🎉 Добро пожаловать в Mini-app ТаксиМакс!\n\n"
            "Mini App работает через веб-интерфейс в Telegram.\n"
            "Для локального теста откройте ссылку в браузере:\n"
            f"http://127.0.0.1:8000\n\n"
            "Или используйте ngrok для тестирования внутри Telegram."
        )
        await Reply(client, user_id).send(text, parse_mode="HTML")

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


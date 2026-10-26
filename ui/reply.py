"""
Удобная обёртка для ответа пользователю: текст + клавиатура.

Инкапсулирует логику отправки сообщений конкретному пользователю и
обработку ошибок.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence

from max_api.client import MaxClient, MaxApiError
from max_api.keyboards import Row, callback_button

logger = logging.getLogger(__name__)

# Весь текст бота размечен HTML-тегами (<b>, <i>, <u> ...), поэтому передаём
# format="html" — без него MAX показывает теги как обычный текст.
HTML_FORMAT = "html"

# Кэш message_id последнего сообщения-меню бота по диалогам (user_id -> mid).
# Позволяет редактировать предыдущее меню (инлайн-навигация) вместо
# отправки нового сообщения каждый раз.
_MENU_MID: Dict[int, str] = {}


def remember_menu_mid(user_id: int, message_id: Any) -> None:
    """Запомнить message_id сообщения-меню для последующего редактирования."""
    if message_id:
        try:
            _MENU_MID[int(user_id)] = str(message_id)
        except (TypeError, ValueError):
            pass


def get_menu_mid(user_id: int) -> Optional[str]:
    """Последнее запомненное message_id меню диалога (или None)."""
    return _MENU_MID.get(int(user_id))


def forget_menu_mid(user_id: int) -> None:
    _MENU_MID.pop(int(user_id), None)


def get_message_mid(update: Any) -> Optional[str]:
    """Вернуть mid сообщения, из которого пришёл апдейт (message_created)."""
    try:
        msg = getattr(update, "message", None)
        if msg is not None:
            mid = getattr(msg, "body_mid", None)
            if mid:
                return str(mid)
        raw = getattr(update, "raw", None)
        if isinstance(raw, dict):
            m = raw.get("message") or {}
            body = m.get("body") if isinstance(m, dict) else None
            if isinstance(body, dict):
                return str(body.get("mid") or "")
            if isinstance(m, dict) and m.get("mid"):
                return str(m["mid"])
    except Exception:
        pass
    return None


async def delete_user_message(client: MaxClient, update: Any) -> bool:
    """Удалить сообщение пользователя, из которого пришёл апдейт."""
    mid = get_message_mid(update)
    if not mid:
        logger.debug("delete_user_message: no mid in update")
        return False
    try:
        await client.delete_message(mid)
        logger.info("delete_user_message: deleted mid=%s", mid)
        return True
    except Exception as exc:
        logger.warning("delete_user_message: failed to delete mid=%s: %s", mid, exc)
        return False


def remember_from_update(user_id: int, update: Any) -> None:
    """Извлечь message_id сообщения с нажатой кнопкой из raw-апдейта.

    При message_callback MAX присылает сообщение, в котором была кнопка —
    запоминаем его mid, чтобы меню обновлялось прямо в нём.
    """
    try:
        raw = getattr(update, "raw", None)
        if not isinstance(raw, dict):
            return
        msg = raw.get("message")
        if isinstance(msg, dict):
            body = msg.get("body")
            mid = body.get("mid") if isinstance(body, dict) else msg.get("mid")
            if mid:
                _MENU_MID[int(user_id)] = str(mid)
                return
        cb = raw.get("callback")
        if isinstance(cb, dict):
            for key in ("message_id", "mid"):
                if cb.get(key):
                    _MENU_MID[int(user_id)] = str(cb[key])
                    return
    except Exception:
        pass


def _extract_mid(result: Any) -> Optional[str]:
    """Достать message_id из ответа send_message (разные формы ответа)."""
    if not isinstance(result, dict):
        return None
    if result.get("message_id"):
        return str(result["message_id"])
    msg = result.get("message")
    if isinstance(msg, dict) and msg.get("message_id"):
        return str(msg["message_id"])
    return None


def escape_html(value: Any) -> str:
    """Экранирует пользовательский ввод перед подстановкой в HTML-текст.

    Без этого адрес/имя вида "<b>тест" сломает разметку сообщения.
    """
    text = "" if value is None else str(value)
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def sanitize_input(value: Any) -> str:
    """Убирает угловые скобки из пользовательского ввода.

    Сообщения отправляются с format="html", поэтому "<b>тест" в имени,
    адресе или телефоне превратился бы в теги и сломал форматирование
    (или «съел» часть текста). Значение при этом остаётся читаемым
    ("Абакан Пушкина 1" не меняется), а "&" не портится.
    """
    text = "" if value is None else str(value)
    return text.replace("<", "").replace(">", "").strip()


class Reply:
    """Помощник отправки сообщения конкретному пользователю.

    ``edit_last=True`` — инлайн-меню: каждое новое сообщение с клавиатурой
    РЕДАКТИРУЕТ предыдущее сообщение-меню бота (одно сообщение в чате
    вместо потока новых). Если редактирование невозможно (сообщение
    удалено и т.п.) — отправляется новое.

    ``default_back`` — payload для кнопки «↪️ Назад», которая автоматически
    добавляется в конец любой клавиатуры, если не передан явно через ``back``.
    """

    def __init__(self, client: MaxClient, user_id: int, edit_last: bool = False,
                 default_back: Optional[str] = None, track_menu: bool = True) -> None:
        self.client = client
        self.user_id = int(user_id)
        self.edit_last = edit_last
        self.default_back = default_back
        # False — это запрос ввода (адрес, гео, текст): новое сообщение,
        # которое не подменяет запомненное меню.
        self.track_menu = track_menu

    async def send(self, text: str, keyboard: Optional[Sequence[Row]] = None,
                   notify: Optional[bool] = None,
                   text_format: Optional[str] = HTML_FORMAT,
                   image_token: Optional[str] = None,
                   replace_previous: Optional[int] = None,
                   edit_last: Optional[bool] = None,
                   back: Optional[str] = None,
                   track_menu: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """Отправить сообщение (или отредактировать предыдущее меню).

        Если ``edit_last`` истинен (или задан по умолчанию в конструкторе)
        и в диалоге есть запомненное сообщение-меню — текст и клавиатура
        записываются В НЁГО через PUT /messages, без создания нового.
        При неудаче редактирования — обычная отправка (фолбэк).

        Если ``replace_previous`` задан — удалит до этого количества ПОСЛЕДНИХ
        сообщений бота в диалоге (только при новой отправке, не при edit).

        Если ``back`` задан (payload для кнопки) — в конец клавиатуры
        добавляется строкой кнопка «↪️ Назад».
        """
        use_edit = self.edit_last if edit_last is None else bool(edit_last)
        remember = self.track_menu if track_menu is None else bool(track_menu)
        attachments: Optional[List[Dict[str, Any]]] = None
        if image_token:
            attachments = [{"type": "image", "payload": {"token": image_token}}]
        if keyboard:
            rows = [list(r) for r in keyboard]
            effective_back = back if back is not None else self.default_back
            _add_back_button(rows, effective_back)
            kb = [{
                "type": "inline_keyboard",
                "payload": {"buttons": rows},
            }]
            attachments = (attachments or []) + kb

        # --- Инлайн-режим: пробуем отредактировать предыдущее меню ---
        if use_edit and keyboard:
            mid = get_menu_mid(self.user_id)
            if mid:
                try:
                    result = await self.client.edit_message(
                        message_id=mid, text=text, attachments=attachments,
                        text_format=text_format,
                    )
                    return result if result else {"message_id": mid}
                except MaxApiError as exc:
                    if "not modified" in str(exc).lower():
                        # Контент идентичен — сообщение уже отображается верно.
                        return {"message_id": mid}
                    logger.warning("Редактирование меню %s не удалось (%s) — отправляем новое сообщение",
                                   mid, exc)
                    # Фолбэк: если редактировать не удалось, шлём новое сообщение,
                    # чтобы пользователь не остался без ответа.
                    # Кэш message_id не обновляем — 다음 попытка снова пойдёт в edit.

        try:
            result = await self.client.send_message(
                user_id=self.user_id, text=text, attachments=attachments,
                notify=notify, text_format=text_format
            )
        except MaxApiError as exc:
            logger.error("Не удалось отправить сообщение %s: %s", self.user_id, exc)
            return None
        if not result:
            return result
        # Запоминаем mid нового сообщения-меню для будущих edit.
        if keyboard and remember:
            mid_new = _extract_mid(result)
            if mid_new:
                remember_menu_mid(self.user_id, mid_new)
        # По желанию удаляем старые исходящие сообщения бота, чтобы чат
        # не разрастался (например, после смены меню).
        if replace_previous:
            asyncio.create_task(self._purge_old_messages(replace_previous))
        return result

    async def _purge_old_messages(self, count: int) -> None:
        """Удалить ``count`` последних сообщений бота в диалоге.

        Сообщения с клавиатурой (callback) пропускаются — пользователь может
        ещё взаимодействовать с кнопками.
        """
        deleted = 0
        try:
            async for msg in self._iter_bot_messages():
                if msg.get("type") == "callback" or msg.get("callback"):
                    continue
                mid = msg.get("body", {}).get("mid") or msg.get("message_id")
                if not mid:
                    continue
                try:
                    await self.client.delete_message(mid)
                    deleted += 1
                except Exception as exc:
                    logger.debug("skip delete %s: %s", mid, exc)
                if deleted >= count:
                    break
        except Exception as exc:
            logger.warning("purge old messages failed: %s", exc)

    async def _iter_bot_messages(self):
        """Yield последовательные сообщения бота (NEWEST first)."""
        async def _page(offset: Optional[int]) -> Dict[str, Any]:
            params: Dict[str, Any] = {"chat_id": self.user_id, "count": 100}
            if offset is not None:
                params["marker"] = offset
            return await self.client._request("GET", "/messages", params=params)
        data = await _page(None)
        for msg in data.get("messages", []):
            yield msg
        while True:
            marker = data.get("marker")
            if not marker:
                break
            data = await _page(marker)
            for msg in data.get("messages", []):
                yield msg

    async def send_geo_button(self, text: str, button_text: str = "📍 Отправить геолокацию",
                              quick: bool = False) -> Optional[Dict[str, Any]]:
        keyboard = [[{"type": "request_geo_location", "text": button_text, "quick": quick}]]
        return await self.send(text, keyboard)

    async def edit(self, message_id: str, text: Optional[str] = None,
                   keyboard: Optional[Sequence[Row]] = None,
                   text_format: Optional[str] = HTML_FORMAT,
                   back: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Редактировать существующее сообщение (menu navigation in-place).

        ``back`` — payload для кнопки «↪️ Назад», добавляемой в конец клавиатуры.
        Если не задан, используется ``self.default_back``.
        """
        attachments: Optional[List[Dict[str, Any]]] = None
        if keyboard is not None:
            rows = [list(r) for r in keyboard]
            effective_back = back if back is not None else self.default_back
            _add_back_button(rows, effective_back)
            attachments = [{"type": "inline_keyboard",
                            "payload": {"buttons": rows}}]
        try:
            return await self.client.edit_message(
                message_id=message_id, text=text, attachments=attachments,
                text_format=text_format,
            )
        except MaxApiError as exc:
            logger.error("Не удалось отредактировать сообщение %s: %s", message_id, exc)
            return None

    async def answer_cb(self, callback_id: str, *,
                        text: Optional[str] = None,
                        notification: Optional[str] = None,
                        keyboard: Optional[Sequence[Row]] = None,
                        back: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Ответить на нажатие кнопки (освобождает состояние кнопки).

        ``back`` — payload для кнопки «↪️ Назад», добавляемой в конец клавиатуры.
        Если не задан, используется ``self.default_back``.
        """
        attachments: Optional[List[Dict[str, Any]]] = None
        if keyboard is not None:
            rows = [list(r) for r in keyboard]
            effective_back = back if back is not None else self.default_back
            _add_back_button(rows, effective_back)
            attachments = [{"type": "inline_keyboard",
                            "payload": {"buttons": rows}}]
        try:
            return await self.client.answer_callback(
                callback_id=callback_id, text=text, notification=notification,
                attachments=attachments,
            )
        except MaxApiError as exc:
            logger.error("Не удалось ответить на callback %s: %s", callback_id, exc)
            return None


def _add_back_button(rows: List[List[Dict[str, Any]]], back: Optional[str]) -> None:
    """Добавить кнопку «↪️ Назад» в конец клавиатуры, если она ещё не присутствует.

    Проверяет по тексту («Назад» / «Главное меню») и payload.
    """
    if not back:
        return
    for row in rows:
        for btn in row:
            if not isinstance(btn, dict):
                continue
            text = btn.get("text", "")
            if "Назад" in text or "Главное меню" in text or btn.get("payload") == back:
                return
    rows.append([{"type": "callback", "text": "↪️ Назад", "payload": back}])


def make_keyboard(rows: Sequence[Row]) -> List[Dict[str, Any]]:
    return [{
        "type": "inline_keyboard",
        "payload": {"buttons": [list(r) for r in rows]},
    }]

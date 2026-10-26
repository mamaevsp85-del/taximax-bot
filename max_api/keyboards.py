"""
Построение inline-клавиатур для MAX Bot API.

В MAX клавиатура — это вложение (attachment) типа inline_keyboard:
{
  "type": "inline_keyboard",
  "payload": {"buttons": [[ {button}, ... ], ...]}
}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

Row = Sequence[Dict[str, Any]]
Keyboard = List[List[Dict[str, Any]]]


def callback_button(text: str, payload: str) -> Dict[str, Any]:
    """Обычная кнопка: при нажатии приходит событие message_callback."""
    return {"type": "callback", "text": text, "payload": payload}


def link_button(text: str, url: str) -> Dict[str, Any]:
    """Кнопка-ссылка."""
    return {"type": "link", "text": text, "url": url}


def request_geo_location_button(text: str, quick: bool = False) -> Dict[str, Any]:
    """Кнопка запроса геолокации (quick=True — быстро без подтверждения)."""
    return {"type": "request_geo_location", "text": text, "quick": quick}


def request_contact_button(text: str) -> Dict[str, Any]:
    """Кнопка запроса контакта (номер телефона пользователя)."""
    return {"type": "request_contact", "text": text}


def inline_keyboard(rows: Sequence[Row]) -> Dict[str, Any]:
    """Собрать вложение inline_keyboard из рядов кнопок."""
    return {
        "type": "inline_keyboard",
        "payload": {"buttons": [list(r) for r in rows]},
    }


def keyboard_attachment(rows: Sequence[Row]) -> List[Dict[str, Any]]:
    """Вернуть список вложений (attachments) с одной клавиатурой."""
    return [inline_keyboard(rows)]


def one_column(buttons: Sequence[Dict[str, Any]]) -> Keyboard:
    """Раскладывает кнопки вертикально (по одной в ряду)."""
    return [[b] for b in buttons]


def chunk(buttons: Sequence[Dict[str, Any]], per_row: int = 2) -> Keyboard:
    """Раскладывает кнопки по рядам по per_row штук."""
    result: Keyboard = []
    for i in range(0, len(buttons), per_row):
        result.append(list(buttons[i:i + per_row]))
    return result


# --- Готовые типовые ряды ---

def nav_row(back_payload: Optional[str] = None, back_text: str = "↪️ Назад",
            home_payload: Optional[str] = None, home_text: str = "🏠 Меню") -> List[Dict[str, Any]]:
    row: List[Dict[str, Any]] = []
    if back_payload:
        row.append(callback_button(back_text, back_payload))
    if home_payload:
        row.append(callback_button(home_text, home_payload))
    return row

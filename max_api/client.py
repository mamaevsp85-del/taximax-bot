"""
Асинхронный HTTP-клиент для MAX Bot API.

Ключевые моменты API:
  - Базовый URL: https://platform-api2.max.ru
  - Токен передаётся в заголовке Authorization (не в query).
  - Отправка: POST /messages?user_id=...  (или ?chat_id=...)
  - Ответ на callback: POST /answers?callback_id=...
  - Long Polling: GET /updates?limit=&timeout=&marker=&types=
  - Лимит: не более 2 сообщений в секунду в один диалог (реализовано через
    локальную очередь с минимальным интервалом).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
import certifi

from config import settings
from .models import Update

logger = logging.getLogger(__name__)


class MaxApiError(Exception):
    """Ошибка вызова MAX API."""


class MaxClient:
    def __init__(self, token: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self.token = token or settings.max_bot_token
        self.base_url = (base_url or settings.max_base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(35.0, connect=15.0),
            trust_env=False,  # не наследовать системный прокси
            verify=certifi.where(),  # явный CA-бандл (Windows часто не находит корневые сертификаты)
        )
        # Минимальный интервал между сообщениями в один диалог
        self._min_interval = max(0.05, float(settings.min_send_interval))
        self._dialog_locks: Dict[int, asyncio.Lock] = {}
        self._last_sent: Dict[int, float] = {}
        self._last_marker: Optional[int] = None

    # --- Низкоуровневый запрос ---
    async def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                       json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"Authorization": self.token, "Content-Type": "application/json"}
        try:
            resp = await self._client.request(method, path, params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise MaxApiError(f"Сетевая ошибка MAX API: {exc}") from exc
        if resp.status_code == 401:
            raise MaxApiError("Ошибка авторизации: токен MAX недействителен")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            raise MaxApiError(f"MAX API {resp.status_code}: {data}")
        return data if isinstance(data, dict) else {"result": data}

    # --- Информация о боте ---
    async def get_me(self) -> Dict[str, Any]:
        return await self._request("GET", "/me")

    async def set_my_commands(self, commands: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Установить команды бота: PATCH /me/commands.

        Формат команды: {"name": "/start", "description": "..."}.
        """
        normalized = [
            {"name": c.get("command") or c.get("name") or "",
             "description": c.get("description") or ""}
            for c in commands
        ]
        return await self._request("PATCH", "/me/commands", json={"commands": normalized})

    # --- Отправка / редактирование / удаление сообщений ---
    async def _throttle(self, dialog_key: int) -> None:
        lock = self._dialog_locks.setdefault(dialog_key, asyncio.Lock())
        async with lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            last = self._last_sent.get(dialog_key, 0.0)
            wait = self._min_interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_sent[dialog_key] = loop.time()

    async def send_message(self, user_id: Optional[int] = None, chat_id: Optional[int] = None,
                           text: Optional[str] = None,
                           attachments: Optional[List[Dict[str, Any]]] = None,
                           notify: Optional[bool] = None,
                           text_format: Optional[str] = None,
                           sound: Optional[str] = None) -> Dict[str, Any]:
        if user_id is None and chat_id is None:
            raise ValueError("Нужно указать user_id или chat_id")
        dialog_key = int(user_id or chat_id)
        await self._throttle(dialog_key)
        params: Dict[str, Any] = {}
        if user_id is not None:
            params["user_id"] = int(user_id)
        if chat_id is not None:
            params["chat_id"] = int(chat_id)
        body: Dict[str, Any] = {}
        if text is not None:
            body["text"] = text[:4000]  # лимит MAX 4000 символов
        if attachments is not None:
            body["attachments"] = attachments
        if notify is not None:
            body["notify"] = notify
        if text_format is not None:
            # Без этого параметра MAX присылает <b> как обычный текст
            body["format"] = text_format
        if sound is not None:
            # Ожидаемый звук push-уведомления (если API MAX принимает).
            # Если поле не поддерживается, MAX отвергнет запрос — тогда ниже
            # будет ошибка MaxApiError, которую вызывающий код ловит и
            # отправляет сообщение без `sound`.
            body["notification"] = {"type": "message", "sound": str(sound)}
        return await self._request("POST", "/messages", params=params, json=body)

    async def edit_message(self, message_id: str, text: Optional[str] = None,
                           attachments: Optional[List[Dict[str, Any]]] = None,
                           text_format: Optional[str] = None) -> Dict[str, Any]:
        params = {"message_id": message_id}
        body: Dict[str, Any] = {}
        if text is not None:
            body["text"] = text[:4000]
        if attachments is not None:
            body["attachments"] = attachments
        if text_format is not None:
            body["format"] = text_format
        return await self._request("PUT", "/messages", params=params, json=body)

    async def delete_message(self, message_id: str) -> Dict[str, Any]:
        return await self._request("DELETE", "/messages", params={"message_id": message_id})

    async def upload_file(self, filename: str, data: bytes,
                          content_type: str = "image/jpeg") -> Dict[str, Any]:
        """Загрузить медиафайл в MAX Bot API (POST /uploads) и получить токен вложения.

        Используется Mini-App (фото авто) и фоновыми задачами (звуковой
        сигнал «нет водителя»). Токен дальше передаётся в attachment
        {"type": "audio"} / {"type": "image"}.
        """
        headers = {"Authorization": self.token}
        try:
            resp = await self._client.post(
                "/uploads", headers=headers,
                files={"file": (filename, data, content_type)},
            )
        except httpx.HTTPError as exc:
            raise MaxApiError(f"Сетевая ошибка MAX API upload: {exc}") from exc
        if resp.status_code == 401:
            raise MaxApiError("Ошибка авторизации: токен MAX недействителен")
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            raise MaxApiError(f"MAX API {resp.status_code}: {payload}")
        # Ответ обычно {"retval": {"token": ...}} — вытаскиваем токен надёжно.
        token = None
        retval = payload.get("retval") if isinstance(payload, dict) else payload
        if isinstance(retval, dict):
            token = retval.get("token") or payload.get("token")
        elif isinstance(payload, dict):
            token = payload.get("token")
        return {"ok": True, "token": token, "raw": payload}

    async def answer_callback(self, callback_id: str, *,
                              text: Optional[str] = None,
                              attachments: Optional[List[Dict[str, Any]]] = None,
                              notification: Optional[str] = None,
                              text_format: Optional[str] = None) -> Dict[str, Any]:
        """Ответ на нажатие кнопки: обновить сообщение и/или показать уведомление."""
        body: Dict[str, Any] = {}
        message: Dict[str, Any] = {}
        if text is not None:
            message["text"] = text[:4000]
        if attachments is not None:
            message["attachments"] = attachments
        if text_format is not None:
            message["format"] = text_format
        if message:
            body["message"] = message
        if notification:
            body["notification"] = notification
        return await self._request(
            "POST", "/answers", params={"callback_id": callback_id}, json=body
        )

    # --- Long Polling ---
    async def get_updates(self, *, limit: int = 100, timeout: int = 30,
                          marker: Optional[int] = None,
                          types: Optional[List[str]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        if types:
            # ВАЖНО: MAX ожидает types ОДНОЙ строкой через запятую
            # (types=message_created,message_callback), НЕ повторяющимися
            # параметрами — иначе фильтр ломается и часть апдейтов не доходит.
            params["types"] = ",".join(types)
        return await self._request("GET", "/updates", params=params)

    @property
    def last_marker(self) -> Optional[int]:
        return self._last_marker

    async def poll_once(self, *, types: Optional[List[str]] = None) -> List[Update]:
        """Один цикл long polling: получить пачку обновлений и запомнить marker."""
        data = await self.get_updates(marker=self._last_marker, types=types)
        marker = data.get("marker")
        if isinstance(marker, int):
            self._last_marker = marker
        return [Update.from_dict(u) for u in data.get("updates", [])]

    async def close(self) -> None:
        await self._client.aclose()


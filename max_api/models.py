"""
Модели данных для разбора обновлений (Update) MAX Bot API.

Основано на официальной документации и реальных payload'ах:
  https://dev.max.ru/docs-api/objects/Update
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class User:
    user_id: int
    name: str = ""
    username: Optional[str] = None
    is_bot: bool = False

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "User":
        data = data or {}
        return cls(
            user_id=int(data.get("user_id") or 0),
            name=data.get("name") or data.get("first_name") or "",
            username=data.get("username"),
            is_bot=bool(data.get("is_bot", False)),
        )


def parse_vcf(vcf: Optional[str]) -> Dict[str, str]:
    """Разбор vcf_info из контакта MAX -> {'name': ..., 'phone': ...}.

    Реальный payload кнопки request_contact выглядит так:
        "payload": {"vcf_info": "BEGIN:VCARD\\r\\nVERSION:3.0\\r\\nTEL;TYPE=cell:79990000000\\r\\nFN:Ivan Ivanov\\r\\nEND:VCARD\\r\\n"}
    Прямого поля "phone" в вложении НЕТ — номер извлекается из VCF.
    """
    result = {"name": "", "phone": ""}
    if not vcf:
        return result
    for raw_line in vcf.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("TEL") and ":" in line:
            phone = line.split(":", 1)[1].strip()
            if phone and not result["phone"]:
                result["phone"] = phone
        elif upper.startswith("FN") and ":" in line:
            name = line.split(":", 1)[1].strip()
            if name:
                result["name"] = name
    return result


@dataclass
class Attachment:
    """Вложение сообщения (image, location, inline_keyboard, contact и т.п.).

    ВАЖНО: в реальных payload'ах MAX поля могут лежать как в `payload`,
    так и на верхнем уровне вложения (например, location:
    {"latitude": ..., "longitude": ..., "type": "location"}).
    """
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def _field(self, name: str) -> Any:
        """Значение поля: сначала из payload, затем с верхнего уровня."""
        if name in self.payload:
            return self.payload[name]
        return self.raw.get(name)

    @property
    def is_location(self) -> bool:
        return self.type == "location"

    @property
    def is_contact(self) -> bool:
        return self.type == "contact"

    @property
    def is_image(self) -> bool:
        return self.type == "image"

    @property
    def image_token(self) -> Optional[str]:
        """Токен загруженного изображения (для пересылки фото админу)."""
        val = self._field("token")
        return str(val) if val else None

    @property
    def image_url(self) -> Optional[str]:
        """Временная ссылка на изображение."""
        val = self._field("url")
        return str(val) if val else None

    @property
    def is_inline_keyboard(self) -> bool:
        return self.type == "inline_keyboard"

    @property
    def latitude(self) -> Optional[float]:
        val = self._field("latitude")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def longitude(self) -> Optional[float]:
        val = self._field("longitude")
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def phone(self) -> Optional[str]:
        """Номер телефона контакта.

        Приоритет: прямое поле phone -> max_info -> разбор vcf_info (VCF).
        """
        val = self._field("phone")
        if val:
            return str(val)
        max_info = self._field("max_info")
        if isinstance(max_info, dict) and max_info.get("phone"):
            return str(max_info["phone"])
        vcf = self._field("vcf_info")
        parsed = parse_vcf(str(vcf) if vcf else None)
        return parsed["phone"] or None

    @property
    def contact_name(self) -> Optional[str]:
        """Имя из контакта: name -> FN из vcf_info."""
        val = self._field("name")
        if val:
            return str(val)
        vcf = self._field("vcf_info")
        parsed = parse_vcf(str(vcf) if vcf else None)
        return parsed["name"] or None

    @property
    def vcf_hash(self) -> Optional[str]:
        """hash для проверки подлинности номера (request_contact)."""
        val = self._field("hash")
        return str(val) if val else None


@dataclass
class Message:
    """Сообщение (Message)."""
    sender: Optional[User] = None
    recipient_user_id: Optional[int] = None
    recipient_chat_id: Optional[int] = None
    body_mid: Optional[str] = None
    text: str = ""
    attachments: List[Attachment] = field(default_factory=list)
    timestamp: int = 0

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Message":
        data = data or {}
        body = data.get("body") or {}
        recipient = data.get("recipient") or {}
        attachments_raw = body.get("attachments") or []
        attachments = [
            Attachment(type=(a.get("type") or ""), payload=(a.get("payload") or {}), raw=a)
            for a in attachments_raw
        ]
        return cls(
            sender=User.from_dict(data.get("sender")),
            recipient_user_id=recipient.get("user_id"),
            recipient_chat_id=recipient.get("chat_id"),
            body_mid=body.get("mid"),
            text=body.get("text") or "",
            attachments=attachments,
            timestamp=int(data.get("timestamp") or 0),
        )

    @property
    def location(self) -> Optional[Attachment]:
        for a in self.attachments:
            if a.is_location:
                return a
        return None

    @property
    def contact(self) -> Optional[Attachment]:
        for a in self.attachments:
            if a.is_contact:
                return a
        return None

    @property
    def image(self) -> Optional[Attachment]:
        for a in self.attachments:
            if a.is_image:
                return a
        return None

    @property
    def has_image(self) -> bool:
        return self.image is not None

    @property
    def has_contact(self) -> bool:
        return self.contact is not None

    @property
    def sender_id(self) -> Optional[int]:
        return self.sender.user_id if self.sender else None


@dataclass
class Callback:
    """Нажатие кнопки (объект callback в Update типа message_callback)."""
    callback_id: str
    payload: str = ""
    user: Optional[User] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Callback":
        data = data or {}
        return cls(
            callback_id=data.get("callback_id") or "",
            payload=data.get("payload") or "",
            user=User.from_dict(data.get("user")),
        )

    @property
    def user_id(self) -> Optional[int]:
        return self.user.user_id if self.user else None


@dataclass
class Update:
    """Обновление от MAX (Webhook или Long Polling)."""
    update_type: str
    timestamp: int
    locale: Optional[str] = None
    chat_id: Optional[int] = None
    user: Optional[User] = None
    message: Optional[Message] = None
    callback: Optional[Callback] = None
    payload: Optional[str] = None  # для bot_started (deep-link payload)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Update":
        message = None
        if isinstance(data.get("message"), dict):
            message = Message.from_dict(data["message"])
        callback = None
        if isinstance(data.get("callback"), dict):
            callback = Callback.from_dict(data["callback"])
        return cls(
            update_type=data.get("update_type") or "",
            timestamp=int(data.get("timestamp") or 0),
            locale=data.get("user_locale"),
            chat_id=data.get("chat_id"),
            user=User.from_dict(data.get("user")) if data.get("user") else None,
            message=message,
            callback=callback,
            payload=data.get("payload"),
            raw=data,
        )

    # --- Удобные свойства ---
    @property
    def is_message_created(self) -> bool:
        return self.update_type == "message_created"

    @property
    def is_message_edited(self) -> bool:
        return self.update_type == "message_edited"

    @property
    def is_message_callback(self) -> bool:
        return self.update_type == "message_callback"

    @property
    def is_bot_started(self) -> bool:
        return self.update_type == "bot_started"

    @property
    def is_bot_added(self) -> bool:
        return self.update_type == "bot_added"

    @property
    def actor_id(self) -> Optional[int]:
        """ID пользователя, инициировавшего событие."""
        if self.callback and self.callback.user_id:
            return self.callback.user_id
        if self.message and self.message.sender_id:
            return self.message.sender_id
        if self.user:
            return self.user.user_id
        return None

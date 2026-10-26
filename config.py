"""
Конфигурация независимого MAX-бота «ТаксиМакс».

Все секреты берутся из файла .env (см. .env.example).
"""
import os
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _detect_root_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


ROOT_DIR = _detect_root_dir()
ENV_FILE_PATH = os.path.join(ROOT_DIR, ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH, case_sensitive=False, extra="ignore"
    )

    # --- MAX ---
    max_bot_token: str = Field(..., min_length=10, description="Токен MAX-бота")
    max_base_url: str = Field(
        default="https://platform-api.max.ru",
        description="Базовый URL MAX Bot API (platform-api2.max.ru из документации не резолвится!)",
    )

    # --- Администраторы и модерация ---
    # Список user_id админов через запятую. Админам доступны все 3 роли.
    admin_user_ids: str = Field(default="", description="Admin MAX user IDs (comma separated)")
    # Ссылка на аккаунт админа в MAX для кнопки «Стать водителем».
    # Например: https://max.ru/admin_username или max.ru/id123
    admin_max_link: Optional[str] = Field(
        default=None, description="Ссылка на аккаунт админа в MAX"
    )
    # Контакт поддержки для пассажиров
    support_contact: str = Field(default="developer@example.com", description="Контакт поддержки")

    # --- Яндекс API (геокодер + маршруты) ---
    yandex_api_key: str = Field(
        default="ccfbaa8f-e397-40df-a1fb-7a2449787f30", description="Yandex API key"
    )
    yandex_geocoder_url: str = "https://geocode-maps.yandex.ru/1.x/"
    yandex_router_url: str = "https://api.routing.yandex.net/v2/route"
    # OpenRouteService (резервный расчёт расстояния). Может быть пустым.
    openrouteservice_key: Optional[str] = None

    # --- Хранилище ---
    storage_file: str = Field(
        default=os.path.join(ROOT_DIR, "data", "taxi_data.json"),
        description="Путь к JSON-файлу данных",
    )

    # --- Города зоны обслуживания ---
    service_area_file: str = Field(
        default=os.path.join(ROOT_DIR, "data", "cities.txt"),
        description="Файл белого списка городов (по одному в строке). Пустой/нет файла = все города разрешены.",
    )

    # --- Поведение ---
    environment: str = "development"
    debug: bool = True
    log_level: str = "INFO"

    # Ограничение частоты исходящих сообщений в один диалог (MAX: 2 сообщения/сек).
    min_send_interval: float = Field(
        default=0.55, description="Мин. интервал между сообщениями в один диалог (сек)"
    )

    @property
    def admin_ids(self) -> List[int]:
        ids: List[int] = []
        for chunk in self.admin_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    ids.append(int(chunk))
                except ValueError:
                    pass
        return ids

    def is_admin(self, user_id: int) -> bool:
        return int(user_id) in self.admin_ids

    # Silent mode - bot doesn't send menus/roles, only processes API requests
    silent_mode: bool = True
    # API Key for authentication
    api_key: str = Field(default="f9LHodD0cOKFnOMeRjCKold8dKcQhCpyb_ThNTfDDOGiRH9F6U8Xqu8TX0QKwiyI8Muc6VJffLERzsYFCfN6", description="API key for authentication")


settings = Settings()

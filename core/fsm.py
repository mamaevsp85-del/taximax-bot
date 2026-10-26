"""
Простой менеджер состояний (FSM) в памяти — аналог aiogram FSM для MAX.

Хранит состояние и данные на пользователя. Достаточно для сценариев:
заказ такси (ввод адресов), профиль водителя, модерация и т.п.

Использование:
    fsm = get_context(user_id)          # синхронно получаем контекст
    await fsm.set_state("passenger:await_pickup")
    await fsm.update_data(pickup=addr)
    data = await fsm.get_data()
    await fsm.clear()
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class FSMContext:
    def __init__(self, store: "_FSMStorage", user_id: int) -> None:
        self._store = store
        self._user_id = int(user_id)

    async def get_state(self) -> Optional[str]:
        return self._store.get_state(self._user_id)

    async def set_state(self, state: Optional[str]) -> None:
        self._store.set_state(self._user_id, state)

    async def update_data(self, **kwargs: Any) -> None:
        self._store.update_data(self._user_id, **kwargs)

    async def get_data(self) -> Dict[str, Any]:
        return self._store.get_data(self._user_id)

    async def clear(self) -> None:
        self._store.clear(self._user_id)


class _FSMStorage:
    """FSM с персистентностью в JSON (данные хранилища).

    Состояния и данные диалогов сохраняются в storage.admin_data['_fsm'],
    поэтому незавершённые сценарии (ввод адреса, заявка водителя и т.п.)
    переживают перезапуск бота. JSON-ключи приводятся к строкам.
    """

    def __init__(self) -> None:
        self._states: Dict[int, str] = {}
        self._data: Dict[int, Dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            from core.storage import storage
            raw = storage.admin_data.get("_fsm", {})
            self._states = {int(k): v for k, v in (raw.get("states") or {}).items()}
            self._data = {int(k): v for k, v in (raw.get("data") or {}).items()}
        except Exception:
            self._states = {}
            self._data = {}

    def _persist(self) -> None:
        try:
            from core.storage import storage
            storage.admin_data["_fsm"] = {
                "states": {str(k): v for k, v in self._states.items()},
                "data": {str(k): v for k, v in self._data.items()},
            }
            storage.save()
        except Exception as exc:  # не роняем бота из-за персистенции FSM
            logger.debug("FSM persist failed: %s", exc)

    # Доступ к состояниям с ленивой загрузкой
    @property
    def _states_map(self) -> Dict[int, str]:
        self._ensure_loaded()
        return self._states

    @property
    def _data_map(self) -> Dict[int, Dict[str, Any]]:
        self._ensure_loaded()
        return self._data

    # Переопределяем прямое обращение для надёжности ленивой загрузки
    def get_state(self, user_id: int) -> Optional[str]:
        return self._states_map.get(int(user_id))

    def set_state(self, user_id: int, state: Optional[str]) -> None:
        self._ensure_loaded()
        if state is None:
            self._states.pop(int(user_id), None)
        else:
            self._states[int(user_id)] = state
        self._persist()

    def update_data(self, user_id: int, **kwargs: Any) -> None:
        self._ensure_loaded()
        self._data.setdefault(int(user_id), {}).update(kwargs)
        self._persist()

    def get_data(self, user_id: int) -> Dict[str, Any]:
        return dict(self._data_map.get(int(user_id), {}))

    def clear(self, user_id: int) -> None:
        self._ensure_loaded()
        self._states.pop(int(user_id), None)
        self._data.pop(int(user_id), None)
        self._persist()


# Глобальный singleton
_store = _FSMStorage()


def get_context(user_id: int) -> FSMContext:
    return FSMContext(_store, user_id)




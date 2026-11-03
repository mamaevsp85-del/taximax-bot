"""Список заказов в боте показывает, сколько заявок ждут водителя."""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _make_update(payload=None, actor=10):
    cb = SimpleNamespace(payload=payload, callback_id="cb-test")
    return SimpleNamespace(
        callback=cb, user=SimpleNamespace(user_id=actor), message=None,
        actor_id=actor, is_bot_started=False, is_bot_added=False,
        is_message_callback=True, is_message_created=False,
        is_message_edited=False, update_type="message_callback")


def _calls_texts(client):
    return [c.kwargs.get("text", "") for c in client.send_message.call_args_list]


class DriverOrdersCountTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "orders_count.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.driver_mod = importlib.import_module("handlers.driver")
        cls.support_mod = importlib.import_module("core.driver_support")
        cls.reply_module = importlib.import_module("ui.reply")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.reply_module._MENU_MID.clear()
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        for mod, name in ((self.storage_module, "storage"),
                          (self.driver_mod, "S"), (self.support_mod, "S")):
            p = patch.object(mod, name, self.s)
            p.start()
            self.addCleanup(p.stop)
        self.client = AsyncMock()
        self.client.send_message.return_value = {"message_id": "m1"}
        self.client.answer_callback = AsyncMock(return_value=True)
        self.client._request = AsyncMock(return_value={"messages": [], "marker": None})
        self._driver(10)
        self.s.ensure_user(20, name="Пассажир")
        self.s.ensure_user(21, name="Пассажир2")

    def _driver(self, did, online=True):
        self.s.ensure_user(did, name=f"Водитель{did}")
        self.s.update_user(did, driver_verification_status="approved",
                           status="online" if online else "offline",
                           car_model="Toyota", car_number="A123BC19")
        self.s.add_access_hours(did, 999)
        return did

    def _order(self, passenger_id=20, status="pending", did=None):
        return self.s.create_order({
            "passenger_id": passenger_id, "driver_id": did,
            "pickup": "Абакан, Ленина 1", "destination": "Абакан, Мира 5",
            "pickup_coords": {"lat": 53.71, "lon": 91.43},
            "destination_coords": {"lat": 53.72, "lon": 91.44},
            "distance": 3.0, "time_minutes": 10, "price": 150,
            "pickup_city": "Абакан", "destination_city": "Абакан",
            "status": status,
        })

    async def test_waiting_count_shown_with_orders(self):
        self._order(20)
        self._order(21)
        await self.driver_mod.driver_orders_available(
            self.client, 10, _make_update("driver:orders_available", 10), {})
        blob = " ".join(_calls_texts(self.client))
        self.assertIn("Ждут водителя сейчас: 2", blob)

    async def test_waiting_count_shown_without_orders(self):
        await self.driver_mod.driver_orders_available(
            self.client, 10, _make_update("driver:orders_available", 10), {})
        blob = " ".join(_calls_texts(self.client))
        self.assertIn("Ждут водителя сейчас: 0", blob)

    async def test_waiting_count_shown_when_offline(self):
        self._order(20)
        self._driver(11, online=False)
        await self.driver_mod.driver_orders_available(
            self.client, 11, _make_update("driver:orders_available", 11), {})
        blob = " ".join(_calls_texts(self.client))
        self.assertIn("Ждут водителя сейчас: 1", blob)
        self.assertIn("выйдите на линию", blob)


if __name__ == "__main__":
    unittest.main()
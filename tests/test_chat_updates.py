"""Уведомления о сообщениях: /api/chat/updates отдаёт только новые.

Проверяем:
  - эндпоинт возвращает сообщения по заказам пользователя;
  - курсор `after` отсекает уже полученные;
  - чужие заказы не попадают в ответ.
"""
import asyncio
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer


class ChatUpdatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "chat.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.api = importlib.import_module("api_server")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.s.ensure_user(20, name="Пассажир")
        self.s.ensure_user(10, name="Водитель")
        self._orig = self.api.storage.storage
        self._orig_max = self.api.maximus_server.S
        self.api.storage.storage = self.s
        self.api.maximus_server.S = self.s

        async def _start():
            app = self.api.create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            return client

        self.loop = asyncio.new_event_loop()
        self.client = self.loop.run_until_complete(_start())

    def tearDown(self):
        async def _close():
            await self.client.close()
        self.loop.run_until_complete(_close())
        self.loop.close()
        self.api.storage.storage = self._orig
        self.api.maximus_server.S = self._orig_max

    def _order_with_chat(self):
        oid = self.s.create_order({
            "passenger_id": 20, "driver_id": 10,
            "pickup": "А", "destination": "Б", "price": 100,
        })
        now = datetime.now()
        self.s.update_order(oid, chat=[
            {"from": 10, "text": "Здравствуйте", "at": (now - timedelta(minutes=2)).isoformat()},
            {"from": 20, "text": "Добрый день", "at": (now - timedelta(minutes=1)).isoformat()},
        ])
        return oid, now

    def _get(self, uid, after=None):
        q = f"/api/chat/updates?uid={uid}"
        if after:
            q += "&after=" + after

        async def _go():
            async with self.client.get(q) as r:
                return r.status, await r.json()
        return self.loop.run_until_complete(_go())

    def test_returns_messages_of_own_orders(self):
        oid, _ = self._order_with_chat()
        st, d = self._get(20)
        self.assertEqual(st, 200)
        self.assertTrue(d.get("ok"))
        texts = [m["text"] for m in d["messages"]]
        self.assertIn("Здравствуйте", texts)
        self.assertIn("Добрый день", texts)
        self.assertTrue(all(m["order_id"] == oid for m in d["messages"]))

    def test_cursor_filters_old(self):
        _, now = self._order_with_chat()
        cursor = (now - timedelta(minutes=1, seconds=30)).isoformat()
        st, d = self._get(20, after=cursor)
        texts = [m["text"] for m in d["messages"]]
        self.assertNotIn("Здравствуйте", texts)   # старее курсора
        self.assertIn("Добрый день", texts)       # новее курсора

    def test_foreign_order_not_returned(self):
        self._order_with_chat()
        self.s.ensure_user(30, name="Посторонний")
        st, d = self._get(30)
        self.assertEqual(d.get("messages"), [])

    def test_empty_for_unknown_user(self):
        st, d = self._get(999999)
        self.assertEqual(st, 200)
        self.assertEqual(d.get("messages"), [])


if __name__ == "__main__":
    unittest.main()
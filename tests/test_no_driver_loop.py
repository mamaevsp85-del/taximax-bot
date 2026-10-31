"""Проверка фонового цикла: вопрос «водитель не найден» реально отправляется.

Запускаем бота-мок и вызываем фоновую функцию один раз, подсунув заказ,
созданный 11 минут назад. Убеждаемся, что пассажиру ушло сообщение с
кнопками «Ждать ещё» / «Отменить» и что отметка о вопросе сохранена.
"""
import asyncio
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


class FakeClient:
    def __init__(self):
        self.sent = []

    async def upload_file(self, filename, data, content_type="audio/wav"):
        return {"ok": True, "token": "audio-token-1"}

    async def send_message(self, user_id=None, chat_id=None, text=None,
                           attachments=None, notify=None, text_format=None, sound=None):
        self.sent.append({
            "user_id": user_id, "text": text,
            "attachments": attachments or [], "notify": notify,
        })
        return {"message_id": "m1"}

    async def close(self):
        pass


class NoDriverLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "loop.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.main = importlib.import_module("main")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.s.ensure_user(20, name="Пассажир")
        self.oid = self.s.create_order({
            "passenger_id": 20, "pickup": "Абакан, Ленина 1",
            "destination": "Черногорск, Юбилейная 29", "price": 300,
        })
        self.s.update_order(
            self.oid,
            created_at=(datetime.now() - timedelta(minutes=11)).isoformat())
        self._orig = self.main.storage.storage
        self.main.storage.storage = self.s
        self.client = FakeClient()

    def tearDown(self):
        self.main.storage.storage = self._orig

    def _ask(self):
        order = self.s.get_order(self.oid)
        ok = asyncio.run(self.main._send_no_driver_question(self.client, order))
        return ok

    def test_question_sent_with_buttons_and_sound(self):
        self.assertTrue(self._ask())
        self.assertEqual(len(self.client.sent), 1)
        msg = self.client.sent[0]
        self.assertEqual(msg["user_id"], 20)
        self.assertIn("Водитель пока не найден", msg["text"])
        self.assertIs(msg["notify"], True)  # полноценный push, а не тишина
        types = [a.get("type") for a in msg["attachments"]]
        self.assertIn("audio", types)          # свой звук
        self.assertIn("inline_keyboard", types)  # ждать / отменить
        kb = next(a for a in msg["attachments"] if a.get("type") == "inline_keyboard")
        payloads = [b.get("payload") for row in kb["payload"]["buttons"] for b in row]
        self.assertIn(f"passenger:no_driver_wait:{self.oid}", payloads)
        self.assertIn(f"passenger:cancel:{self.oid}", payloads)

    def test_order_selected_by_loop_predicate(self):
        ids = self.s.get_pending_no_driver_orders(
            first_ask_minutes=10, ask_interval_minutes=10)
        self.assertEqual(ids, [self.oid])

    def test_ask_then_repeat_interval(self):
        self._ask()
        self.s.mark_no_driver_asked(self.oid)
        # сразу после вопроса повтор не нужен
        self.assertEqual(self.s.get_pending_no_driver_orders(10, 10), [])
        # через 11 минут после вопроса — снова пора спрашивать
        self.s.update_order(
            self.oid,
            no_driver_asked_at=(datetime.now() - timedelta(minutes=11)).isoformat())
        self.assertEqual(self.s.get_pending_no_driver_orders(10, 10), [self.oid])

    def test_unanswered_order_cancelled(self):
        self.s.mark_no_driver_asked(self.oid)
        self.s.update_order(
            self.oid,
            no_driver_asked_at=(datetime.now() - timedelta(minutes=16)).isoformat())
        cancelled = self.s.cancel_unanswered_no_driver(
            minutes_after_ask=self.main.NO_DRIVER_UNANSWERED_MAX_MIN)
        self.assertIn(self.oid, cancelled)
        order = self.s.get_order(self.oid)
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["cancelled_by"], "no_driver_unanswered")


if __name__ == "__main__":
    unittest.main()
"""Уведомление «водитель не найден: ждать или отменить?».

Проверяем:
  - вопрос задаётся не раньше 10 минут ожидания и повторяется не чаще раза в 10 минут;
  - «Ждать ещё» снимает авто-отмену (passenger_waiting=True);
  - заказ без ответа отменяется автоматически;
  - заказ, где пассажир выбрал ждать, не отменяется;
  - WAV-сигнал «такси ждёт» генерируется корректно (это и есть «свой звук»).
"""
import importlib
import io
import os
import tempfile
import unittest
import wave
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


class NoDriverAskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "nodriver.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.s.ensure_user(20, name="Пассажир")
        self.oid = self.s.create_order({
            "passenger_id": 20, "pickup": "А", "destination": "Б", "price": 100,
        })

    def _age(self, minutes):
        """Сдвинуть created_at заказа назад."""
        created = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        self.s.update_order(self.oid, created_at=created)

    def test_not_asked_before_10_minutes(self):
        self._age(5)
        self.assertEqual(
            self.s.get_pending_no_driver_orders(first_ask_minutes=10,
                                                ask_interval_minutes=10), [])

    def test_asked_after_10_minutes(self):
        self._age(11)
        ids = self.s.get_pending_no_driver_orders(first_ask_minutes=10,
                                                  ask_interval_minutes=10)
        self.assertEqual(ids, [self.oid])

    def test_not_repeated_immediately(self):
        self._age(11)
        self.s.mark_no_driver_asked(self.oid)
        # сразу после вопроса — повтора нет
        self.assertEqual(
            self.s.get_pending_no_driver_orders(first_ask_minutes=10,
                                                ask_interval_minutes=10), [])
        # через 11 минут после вопроса — повтор есть
        asked = (datetime.now() - timedelta(minutes=11)).isoformat()
        self.s.update_order(self.oid, no_driver_asked_at=asked)
        self.assertEqual(
            self.s.get_pending_no_driver_orders(first_ask_minutes=10,
                                                ask_interval_minutes=10), [self.oid])

    def test_order_with_driver_not_asked(self):
        self._age(20)
        self.s.update_order(self.oid, driver_id=10)
        self.assertEqual(
            self.s.get_pending_no_driver_orders(first_ask_minutes=10,
                                                ask_interval_minutes=10), [])

    def test_cancelled_when_unanswered(self):
        self._age(30)
        self.s.mark_no_driver_asked(self.oid)
        self.s.update_order(
            self.oid,
            no_driver_asked_at=(datetime.now() - timedelta(minutes=20)).isoformat())
        cancelled = self.s.cancel_unanswered_no_driver(minutes_after_ask=15)
        self.assertIn(self.oid, cancelled)
        self.assertEqual(self.s.get_order(self.oid).get("status"), "cancelled")

    def test_waiting_passenger_not_cancelled(self):
        self._age(30)
        self.s.update_order(
            self.oid,
            no_driver_asked_at=(datetime.now() - timedelta(minutes=20)).isoformat())
        self.s.set_passenger_waiting(self.oid, True)
        cancelled = self.s.cancel_unanswered_no_driver(minutes_after_ask=15)
        self.assertNotIn(self.oid, cancelled)
        self.assertEqual(self.s.get_order(self.oid).get("status"), "pending")

    def test_sound_wav_is_valid(self):
        """Файл «такси ждёт» — корректный WAV, его можно приложить к сообщению."""
        main = importlib.import_module("main")
        data = main._no_driver_sound_wav()
        self.assertGreater(len(data), 1000)
        self.assertEqual(data[:4], b"RIFF")
        with wave.open(io.BytesIO(data), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertGreater(w.getnframes(), 10000)


if __name__ == "__main__":
    unittest.main()
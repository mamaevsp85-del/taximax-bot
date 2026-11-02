"""Ожидание в пути: статус заказа НЕ меняется, повторный старт безопасен."""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WaitingInTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "wt.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.support = importlib.import_module("core.driver_support")
        cls.maximus = importlib.import_module("maximus_server")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.s.ensure_user(10, name="Водитель")
        self.s.ensure_user(20, name="Пассажир")
        self.s.update_user(10, driver_verification_status="approved",
                           status="online", car_model="Kia", car_number="А111АА 19")
        self._o_max = self.maximus.S
        self.maximus.S = self.s
        self._o_sup = self.support.S
        self.support.S = self.s
        oid = self.s.create_order({
            "passenger_id": 20, "driver_id": 10, "price": 300,
            "pickup": "А", "destination": "Б", "pickup_city": "Черногорск",
            "status": "in_progress",
        })
        self.s.update_user(10, driver_current_order_id=oid)
        self.oid = oid

    def tearDown(self):
        self.maximus.S = self._o_max
        self.support.S = self._o_sup

    def _running(self):
        o = self.s.get_order(self.oid) or {}
        return bool(o.get("waiting_started_at") and not o.get("waiting_ended_at"))

    # --- 1) статус не меняется ---
    def test_wait_does_not_change_status(self):
        res = self.maximus.act(10, {"op": "driver.wait", "id": self.oid})
        self.assertTrue(res["ok"])
        self.assertEqual(self.s.get_order(self.oid)["status"], "in_progress")
        self.assertTrue(self._running())

    def test_second_wait_stops_timer_and_keeps_status(self):
        self.maximus.act(10, {"op": "driver.wait", "id": self.oid})
        self.assertTrue(self._running())
        self.maximus.act(10, {"op": "driver.wait", "id": self.oid})
        self.assertFalse(self._running(), "второе нажатие должно ОСТАНОВИТЬ ожидание")
        self.assertEqual(self.s.get_order(self.oid)["status"], "in_progress")

    # --- 2) повторный старт не сбрасывает ожидание ---
    def test_repeated_start_keeps_waiting(self):
        self.maximus.act(10, {"op": "driver.wait", "id": self.oid})
        self.assertTrue(self._running())
        res = self.maximus.act(10, {"op": "driver.start", "id": self.oid})
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("already"))
        self.assertTrue(self._running(),
                        "повторное «Начать движение» не должно останавливать ожидание")
        self.assertEqual(self.s.get_order(self.oid)["status"], "in_progress")

    # --- 3) ответ содержит состояние заказа ---
    def test_wait_returns_order_and_waiting(self):
        res = self.maximus.act(10, {"op": "driver.wait", "id": self.oid})
        self.assertIn("waiting", res)
        self.assertIn("order", res)
        self.assertEqual(res["order"]["status"], "in_progress")
        self.assertTrue(res["waiting"]["running"])

    # --- 4) на завершении ожидание фиксируется ---
    def test_complete_stops_waiting(self):
        self.maximus.act(10, {"op": "driver.wait", "id": self.oid})
        self.maximus.act(10, {"op": "driver.complete", "id": self.oid})
        order = self.s.get_order(self.oid)
        self.assertIn(order["status"], ("completed", "in_progress"))
        self.assertIsNone(order.get("waiting_started_at"))

    # --- 5) ожидание доступно только в поездке ---
    def test_wait_rejected_before_trip(self):
        oid2 = self.s.create_order({
            "passenger_id": 20, "driver_id": 10, "price": 100,
            "pickup": "А", "destination": "Б", "status": "accepted",
        })
        with self.assertRaises(ValueError):
            self.maximus.act(10, {"op": "driver.wait", "id": oid2})


if __name__ == "__main__":
    unittest.main()
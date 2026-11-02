"""Ожидание: таймер стартует при «Прибыл», настройки берутся из тарифа."""
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


class WaitingTimerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "wait.json"),
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
        self._orig = self.maximus.S
        self.maximus.S = self.s
        self._orig_sup = self.support.S
        self.support.S = self.s
        self.s.admin_data["tariff_intercity"] = {}
        self.s.admin_data["tariffs_by_city"] = {}

    def tearDown(self):
        self.maximus.S = self._orig
        self.support.S = self._orig_sup

    def _order(self, status="accepted", city="Черногорск"):
        oid = self.s.create_order({
            "passenger_id": 20, "driver_id": 10, "price": 200,
            "pickup": "А", "destination": "Б", "pickup_city": city,
            "status": status,
        })
        self.s.update_user(10, driver_current_order_id=oid)
        return oid

    # ---------- 2) «Прибыл» запускает таймер ----------
    def test_arrived_starts_waiting_timer(self):
        oid = self._order()
        res = self.maximus.act(10, {"op": "driver.arrived", "id": oid})
        self.assertTrue(res["ok"])
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "driver_arrived")
        self.assertTrue(order.get("waiting_started_at"))
        self.assertIsNone(order.get("waiting_ended_at"))
        self.assertIn("waiting", res)
        self.assertTrue(res["waiting"]["running"])

    def test_waiting_visible_in_brief_order(self):
        oid = self._order()
        self.maximus.act(10, {"op": "driver.arrived", "id": oid})
        brief = self.maximus.brief_order(self.s.get_order(oid))
        w = brief["waiting"]
        self.assertTrue(w["running"])
        self.assertGreaterEqual(w["seconds"], 0)
        self.assertIn("free_minutes", w)
        self.assertIn("per_minute", w)

    # ---------- 3) настройки из тарифа ----------
    def test_defaults_when_no_settings(self):
        cfg = self.support.get_waiting_settings("Черногорск")
        self.assertEqual(cfg["free_minutes"], 3)
        self.assertEqual(cfg["per_minute"], 5)

    def test_admin_saves_waiting_settings(self):
        self.maximus.admin_act({
            "op": "admin.tariff.waiting",
            "free_minutes": 7, "per_minute": 12,
        })
        cfg = self.support.get_waiting_settings("")
        self.assertEqual(cfg["free_minutes"], 7)
        self.assertEqual(cfg["per_minute"], 12)

    def test_waiting_cost_uses_admin_settings(self):
        """1 бесплатная минута и 10 ₽/мин → 3 мин ожидания = 20 ₽."""
        self.maximus.admin_act({
            "op": "admin.tariff.waiting",
            "free_minutes": 1, "per_minute": 10,
        })
        oid = self._order()
        started = (datetime.now() - timedelta(minutes=3)).isoformat()
        self.s.update_order(oid, status="driver_arrived",
                            waiting_started_at=started, waiting_ended_at=None)
        info = self.support.get_waiting_info(oid)
        self.assertFalse(info["is_free"])
        self.assertAlmostEqual(info["cost"], 20.0, places=1)
        self.assertEqual(info["free_minutes"], 1)
        self.assertEqual(info["per_minute"], 10)

    def test_free_period_no_charge(self):
        self.maximus.admin_act({
            "op": "admin.tariff.waiting", "free_minutes": 5, "per_minute": 10,
        })
        oid = self._order()
        started = (datetime.now() - timedelta(minutes=2)).isoformat()
        self.s.update_order(oid, status="driver_arrived",
                            waiting_started_at=started, waiting_ended_at=None)
        info = self.support.get_waiting_info(oid)
        self.assertTrue(info["is_free"])
        self.assertEqual(info["cost"], 0)

    def test_city_specific_waiting_settings(self):
        self.maximus.admin_act({
            "op": "admin.tariff.waiting", "city": "Абакан",
            "free_minutes": 10, "per_minute": 20,
        })
        city_cfg = self.support.get_waiting_settings("Абакан")
        self.assertEqual(city_cfg["free_minutes"], 10)
        self.assertEqual(city_cfg["per_minute"], 20)

    def test_start_stops_waiting(self):
        oid = self._order()
        self.maximus.act(10, {"op": "driver.arrived", "id": oid})
        self.maximus.act(10, {"op": "driver.start", "id": oid})
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "in_progress")
        self.assertIsNone(order.get("waiting_started_at"))


if __name__ == "__main__":
    unittest.main()
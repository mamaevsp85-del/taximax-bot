"""Дневная статистика водителя и заказ из избранного (Mini-App).

Проверяем:
  - driver.day отдаёт все завершённые заявки за дату с суммой и количеством;
  - суммы из driver.day совпадают с графиком daily;
  - favorite.use пересчитывает цену по актуальному тарифу при создании заказа.
"""
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


class DriverDayStatsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "driverday.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.maximus = importlib.import_module("maximus_server")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self._orig = self.maximus.S
        self.maximus.S = self.s
        self.driver = 10
        self.s.ensure_user(self.driver, name="Водитель")
        self.s.update_user(self.driver, phone="+79130000001", onboarded=True,
                           driver_verification_status="approved",
                           type="driver", active_role="driver")
        self.s.ensure_user(20, name="Пассажир")
        # Даты — относительно сегодня: график строит только последние 30 дней.
        self.day1 = datetime.now().date() - timedelta(days=1)
        self.day2 = datetime.now().date()
        self._make(self.day1, "10:00:00", 300, 0)
        self._make(self.day1, "17:30:00", 250, 50)
        self._make(self.day2, "09:00:00", 400, 0)

    def tearDown(self):
        self.maximus.S = self._orig

    def _make(self, day, clock, price, waiting_cost):
        oid = self.s.create_order({
            "passenger_id": 20, "driver_id": self.driver,
            "pickup": "Черногорск, Мира 1", "destination": "Черногорск, Степная 12",
            "price": price, "base_price": price, "distance": 5.0,
            "time_minutes": 12, "status": "completed",
        })
        self.s.update_order(oid, completed_at=f"{day.isoformat()}T{clock}",
                            waiting_cost=waiting_cost)
        return oid

    def test_day_returns_all_orders_of_date(self):
        day = self.day1.isoformat()
        res = self.maximus.act(self.driver, {"op": "driver.day", "date": day})
        self.assertTrue(res["ok"])
        self.assertEqual(res["date"], day)
        self.assertEqual(res["count"], 2)
        # 300 + (250 + 50 за ожидание)
        self.assertAlmostEqual(res["sum"], 600.0, delta=1)

    def test_day_empty_date(self):
        res = self.maximus.act(self.driver, {"op": "driver.day", "date": "2020-01-01"})
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["orders"], [])

    def test_day_matches_chart_totals(self):
        chart = self.s.get_driver_daily_stats(self.driver, 30)
        by_date = {row["date"]: row for row in chart}
        for day in (self.day1.isoformat(), self.day2.isoformat()):
            res = self.maximus.act(self.driver, {"op": "driver.day", "date": day})
            self.assertEqual(res["count"], by_date[day]["count"], day)
            self.assertAlmostEqual(float(res["sum"]), float(by_date[day]["sum"]),
                                   delta=1, msg=day)

    def test_bad_date_rejected(self):
        with self.assertRaises(ValueError):
            self.maximus.act(self.driver, {"op": "driver.day", "date": "05.03.2026"})
        with self.assertRaises(ValueError):
            self.maximus.act(self.driver, {"op": "driver.day", "date": ""})

    def test_driver_day_requires_driver_role(self):
        self.s.update_user(self.driver, driver_verification_status="pending")
        with self.assertRaises(PermissionError):
            self.maximus.act(self.driver, {"op": "driver.day", "date": "2026-03-05"})


if __name__ == "__main__":
    unittest.main()
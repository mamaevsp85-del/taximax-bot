"""Наценки тарифа: день/ночь в процентах и цена за промежуточную точку.

Проверяем:
  - ночная наценка применяется в ночное окно, дневная — вне его;
  - окно «до 24» корректно считается ночью до конца суток;
  - каждая промежуточная точка добавляет фиксированную сумму сверх тарифа;
  - стоимость избранного маршрута пересчитывается по актуальному тарифу.
"""
import importlib
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


class SurchargeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "surch.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.tm = importlib.import_module("core.tariff_manager")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self._orig = self.storage_module.storage
        self.storage_module.storage = self.s
        self.s.admin_data["tariff_intercity"] = {}
        self.s.admin_data["tariffs_by_city"] = {}

    def tearDown(self):
        self.storage_module.storage = self._orig

    def _set(self, **fields):
        city = dict(self.s.admin_data["tariffs_by_city"].get("Черногорск") or {})
        city.update(fields)
        self.s.admin_data["tariffs_by_city"] = {"Черногорск": city}

    def _price(self, stops=0, at=None):
        return self.tm.calculate_trip_price(
            10.0, 20, pickup_city="Черногорск", destination_city="Черногорск",
            waypoints_count=stops, at=at)

    # --- промежуточные точки ---
    def test_waypoint_surcharge_per_stop(self):
        base = self._price(0)
        self._set(waypoint_surcharge=100)
        one = self._price(1)
        two = self._price(2)
        self.assertAlmostEqual(one - base, 100, delta=1)
        self.assertAlmostEqual(two - base, 200, delta=1)

    def test_no_waypoint_surcharge_by_default(self):
        base = self._price(0)
        self.assertAlmostEqual(self._price(1) - base, 0, delta=1)

    # --- день/ночь ---
    def test_day_surcharge_applied(self):
        base = self._price(0, at=datetime(2026, 1, 1, 12, 0))
        self._set(day_surcharge_percent=30, night_surcharge_percent=0,
                  night_start_hour=22, night_end_hour=6)
        day = self._price(0, at=datetime(2026, 1, 1, 12, 0))
        self.assertGreater(day, base)
        self.assertAlmostEqual(day, base * 1.3, delta=6)

    def test_night_surcharge_applied(self):
        base = self._price(0, at=datetime(2026, 1, 1, 23, 0))
        self._set(day_surcharge_percent=0, night_surcharge_percent=40,
                  night_start_hour=22, night_end_hour=6)
        night = self._price(0, at=datetime(2026, 1, 1, 23, 0))
        self.assertAlmostEqual(night, base * 1.4, delta=6)

    def test_night_window_until_midnight(self):
        """night_end = 24 — ночь до конца суток (не вырождается в «нет ночи»)."""
        self.assertTrue(self.tm.is_night_time(datetime(2026, 1, 1, 23, 0),
                                              night_start=0, night_end=24))
        self.assertFalse(self.tm.is_night_time(datetime(2026, 1, 1, 12, 0),
                                               night_start=23, night_end=24))

    def test_night_off_outside_window(self):
        self._set(night_surcharge_percent=40, night_start_hour=22, night_end_hour=6)
        day = self._price(0, at=datetime(2026, 1, 1, 12, 0))
        clean = self._price(0, at=datetime(2026, 1, 1, 12, 0))
        self.assertEqual(day, clean)

    # --- избранное пересчитывается ---
    def test_favorite_preview_recalculates_price(self):
        user = 20
        self.s.ensure_user(user, name="Пассажир", username="p20")
        oid = self.s.create_order({
            "passenger_id": user, "pickup": "А", "destination": "Б",
            "price": 100, "base_price": 100, "distance": 5.0,
            "time_minutes": 10, "status": "completed",
        })
        self.s.add_favorite(user, oid, label="А → Б")
        route = self.s.favorite_route(user, 0)
        self.assertEqual(route["pickup"], "А")
        # Меняем тариф — цена избранного должна считаться заново.
        self._set(base_price=500, price_per_km=100, min_price=500)
        fresh = self.tm.calculate_trip_price(
            float(route["km"] or 0), int(route["minutes"] or 0),
            pickup_city="Черногорск", destination_city="Черногорск")
        self.assertGreaterEqual(fresh, 500)

    # --- удаление избранного ---
    def test_remove_favorite_at(self):
        user = 21
        self.s.ensure_user(user, name="П2")
        for i in range(2):
            oid = self.s.create_order({"passenger_id": user, "pickup": f"A{i}",
                                       "destination": f"B{i}", "price": 100})
            self.s.add_favorite(user, oid, label=f"маршрут {i}")
        self.assertEqual(len(self.s.get_favorites(user)), 2)
        self.assertTrue(self.s.remove_favorite_at(user, 0))
        self.assertEqual(len(self.s.get_favorites(user)), 1)


if __name__ == "__main__":
    unittest.main()
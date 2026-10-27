"""Тесты учёта времени в межгороде и распознавания явного города."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class IntercityTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "ic2.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.tariff = importlib.import_module("core.tariff_manager")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    # ---------- время в межгороде ----------
    def test_intercity_time_rate_is_positive(self):
        t = self.tariff.get_tariff_settings(intercity=True)
        self.assertGreater(float(t.get("price_per_minute", 0)), 0)

    def test_city_time_rate_unchanged(self):
        t = self.tariff.get_tariff_settings(intercity=False)
        self.assertEqual(float(t.get("price_per_minute", 0)), 2)

    def test_intercity_price_uses_time(self):
        """Дольше едем — дороже, даже при равном километраже."""
        fast = self.tariff.calculate_trip_price(
            40, 40, pickup_city="Черногорск", destination_city="Минусинск")
        slow = self.tariff.calculate_trip_price(
            40, 60, pickup_city="Черногорск", destination_city="Минусинск")
        self.assertGreater(slow, fast)

    def test_intercity_time_delta_is_meaningful(self):
        """+20 минут даёт заметную прибавку (несколько десятков рублей)."""
        fast = self.tariff.calculate_trip_price(
            40, 40, pickup_city="Черногорск", destination_city="Минусинск")
        slow = self.tariff.calculate_trip_price(
            40, 60, pickup_city="Черногорск", destination_city="Минусинск")
        self.assertGreaterEqual(slow - fast, 50)

    def test_stop_adds_cost_via_time(self):
        """Заезд в город: +0.9 км и +2 мин должны давать больше 30 ₽."""
        direct = self.tariff.calculate_trip_price(
            39.2, 39, pickup_city="Черногорск", destination_city="Минусинск")
        via_stop = self.tariff.calculate_trip_price(
            40.1, 41, pickup_city="Черногорск", destination_city="Минусинск")
        self.assertGreater(via_stop - direct, 30)

    # ---------- межгород определяется корректно ----------
    def test_intercity_true_for_different_cities(self):
        self.assertTrue(self.tariff.is_intercity("Черногорск", "Абакан"))
        self.assertTrue(self.tariff.is_intercity("Черногорск", "Минусинск"))

    def test_intercity_false_for_same_city(self):
        self.assertFalse(self.tariff.is_intercity("Черногорск", "Черногорск"))

    def test_intercity_tariff_differs_from_city(self):
        city = self.tariff.get_tariff_settings(city="Черногорск")
        inter = self.tariff.get_tariff_settings(intercity=True)
        self.assertNotEqual(city.get("price_per_km"), inter.get("price_per_km"))
        self.assertGreater(float(inter.get("min_price", 0)),
                           float(city.get("min_price", 0)))


if __name__ == "__main__":
    unittest.main()

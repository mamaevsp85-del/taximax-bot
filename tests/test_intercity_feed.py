"""Межгород считается по тарифу межгорода + живой фид заказов у водителя."""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class IntercityAndFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "ic.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.tariff = importlib.import_module("core.tariff_manager")
        cls.support = importlib.import_module("core.driver_support")
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
        self._orig_support = self.support.S
        self.support.S = self.s
        # get_available_orders_for_driver и driver_feed читают ГЛОБАЛЬНОЕ
        # хранилище модуля — подменяем и его, иначе тест видит пустую базу.
        self._orig_global = self.storage_module.storage
        self.storage_module.storage = self.s
        self.driver = 10
        self.s.ensure_user(self.driver, name="Водитель")
        self.s.update_user(self.driver, phone="+79130000001", onboarded=True,
                           driver_verification_status="approved",
                           type="driver", active_role="driver", status="online")
        self.s.ensure_user(20, name="Пассажир")
        self.s.update_user(20, phone="+79130000002", onboarded=True,
                           home_city="Черногорск")
        # Настройки тарифа как в боевой базе: межгород 90 ₽ + 40 ₽/км + 5 ₽/мин.
        self.s.admin_data["tariff_intercity"] = {
            "base_price": 90, "included_km": 2, "price_per_km": 40,
            "price_per_minute": 5, "min_price": 90,
            "rounding_type": "up", "rounding_step": 10,
            "night_surcharge_percent": 0, "day_surcharge_percent": 0,
        }
        self.s.admin_data["tariffs_by_city"] = {
            "черногорск": {
                "base_price": 90, "included_km": 2, "price_per_km": 20,
                "price_per_minute": 4, "min_price": 90,
                "rounding_type": "up", "rounding_step": 10,
                "night_surcharge_percent": 0, "day_surcharge_percent": 0,
            },
        }

    def tearDown(self):
        self.maximus.S = self._orig
        self.support.S = self._orig_support
        self.storage_module.storage = self._orig_global

    # ---------- межгород ----------
    def test_intercity_tariff_used_for_different_cities(self):
        # Берём реальные цифры из настроенного тарифа межгорода
        # (90 ₽ + 40 ₽/км + 5 ₽/мин) и сравниваем с городским.
        price = self.tariff.calculate_trip_price(
            13.2, 13, pickup_city="Абакан", destination_city="Черногорск")
        city = self.tariff.calculate_trip_price(
            13.2, 13, pickup_city="Черногорск", destination_city="Черногорск")
        # Межгород обязан быть заметно дороже городской поездки.
        self.assertGreater(price, city)
        # И не меньше, чем 90 + (13.2 - 2) * 40 (без минутной части).
        self.assertGreaterEqual(price, 90 + (13.2 - 2) * 40)

    def test_city_tariff_cheaper_per_km(self):
        ic = self.tariff.get_tariff_settings(intercity=True)
        city = self.tariff.get_tariff_settings(city="Черногорск")
        self.assertGreater(float(ic.get("price_per_km") or 0),
                           float(city.get("price_per_km") or 0))

    def test_explicit_city_detected_in_address(self):
        self.assertTrue(self.maximus._has_explicit_other_city(
            "Торосова 15, Абакан", "Черногорск"))
        self.assertFalse(self.maximus._has_explicit_other_city(
            "Торосова 15", "Черногорск"))

    def test_quote_intercity_live(self):
        """Живой расчёт: Абакан -> Черногорск должен идти по межгороду."""
        res = self.maximus.act(20, {
            "op": "quote",
            "pickup": "Торосова 15, Абакан",
            "destination": "Южная 1, Черногорск",
        })
        if not res.get("ok"):
            self.skipTest(f"геокодер недоступен: {res.get('error')}")
        draft = res.get("draft") or {}
        self.assertTrue(
            draft.get("is_intercity"),
            f"не распознан межгород: {draft.get('pickup_city')} -> "
            f"{draft.get('destination_city')}")
        km = float(draft.get("km") or 0)
        mins = int(draft.get("minutes") or 0)
        expected = 90 + max(0, km - 2) * 40 + mins * 5
        # Допуск на округление вверх до 10.
        self.assertGreaterEqual(float(draft.get("price") or 0), expected - 10)

    # ---------- живой фид ----------
    def _order(self, passenger=20, pickup="Черногорск, Мира 1",
               destination="Черногорск, Юбилейная 3", lat=53.82, lon=91.29):
        return self.s.create_order({
            "passenger_id": passenger,
            "pickup": pickup, "destination": destination,
            "pickup_city": "Черногорск", "destination_city": "Черногорск",
            "pickup_coords": {"lat": lat, "lon": lon},
            "destination_coords": {"lat": lat + 0.01, "lon": lon + 0.01},
            "price": 150, "base_price": 150, "distance": 2.0,
            "time_minutes": 5, "status": "pending",
        })

    def test_feed_returns_only_light_fields(self):
        feed = self.maximus.driver_feed(self.driver)
        self.assertTrue(feed["ok"])
        for key in ("available", "available_count", "pending_waiting",
                    "groups", "reference", "current", "next", "line"):
            self.assertIn(key, feed)
        for heavy in ("history", "daily", "stats", "transactions", "today_orders"):
            self.assertNotIn(heavy, feed)

    def test_feed_shows_new_order_immediately(self):
        self.assertEqual(self.maximus.driver_feed(self.driver)["available_count"], 0)
        oid = self._order()
        feed = self.maximus.driver_feed(self.driver)
        self.assertEqual(feed["available_count"], 1)
        self.assertEqual(feed["available"][0]["id"], oid)
        self.assertEqual(feed["pending_waiting"], 1)

    def test_feed_drops_order_when_cancelled(self):
        oid = self._order()
        self.assertEqual(self.maximus.driver_feed(self.driver)["available_count"], 1)
        self.s.update_order(oid, status="cancelled")
        self.assertEqual(self.maximus.driver_feed(self.driver)["available_count"], 0)

    def test_feed_nearest_first(self):
        self.s.update_user(self.driver, last_lat=53.80, last_lon=91.30)
        near = self._order(pickup="Черногорск, близко", lat=53.801, lon=91.301)
        far = self._order(passenger=999, pickup="Абакан, далеко",
                          lat=53.72, lon=91.42)
        ids = [o["id"] for o in self.maximus.driver_feed(self.driver)["available"]]
        self.assertEqual(ids, [near, far])

    def test_feed_requires_driver_role(self):
        with self.assertRaises(PermissionError):
            self.maximus.act(20, {"op": "driver.feed"})


if __name__ == "__main__":
    unittest.main()
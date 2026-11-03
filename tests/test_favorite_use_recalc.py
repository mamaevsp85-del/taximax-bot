"""Заказ из избранного всегда пересчитывается по АКТУАЛЬНОМУ тарифу."""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FavoriteUseRecalcTests(unittest.TestCase):
    """Заказ из избранного всегда создаётся по АКТУАЛЬНОМУ тарифу."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "favuse.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.maximus = importlib.import_module("maximus_server")
        cls.tm = importlib.import_module("core.tariff_manager")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self._orig_storage = self.storage_module.storage
        self._orig_maximus = self.maximus.S
        self.storage_module.storage = self.s
        self.maximus.S = self.s
        self.user = 20
        self.s.ensure_user(self.user, name="Пассажир")
        self.s.update_user(self.user, phone="+79130000000", onboarded=True,
                           home_city="Черногорск")
        self.oid = self.s.create_order({
            "passenger_id": self.user, "pickup": "Черногорск, Мира 1",
            "destination": "Черногорск, Степная 12",
            "price": 100, "base_price": 100, "distance": 5.0,
            "time_minutes": 10, "status": "completed",
        })
        self.s.add_favorite(self.user, self.oid, label="Работа")

    def tearDown(self):
        self.maximus.S = self._orig_maximus
        self.storage_module.storage = self._orig_storage

    def _set_tariff(self, base, per_km):
        self.s.admin_data["tariffs_by_city"] = {
            "Черногорск": {"base_price": base, "price_per_km": per_km,
                           "price_per_minute": 0, "min_price": base},
        }

    def _fake_quote(self, *args, **kwargs):
        # Пересчёт по текущему тарифу (как настоящий quote, но без сети):
        # 5 км, 10 минут, без остановок, Черногорск.
        price = self.tm.calculate_trip_price(
            5.0, 10, pickup_city="Черногорск", destination_city="Черногорск",
            waypoints_count=0)
        return {
            "pickup": "Черногорск, Мира 1",
            "destination": "Черногорск, Степная 12",
            "pickup_coords": {"lat": 53.82, "lon": 91.33},
            "destination_coords": {"lat": 53.81, "lon": 91.29},
            "pickup_city": "Черногорск", "destination_city": "Черногорск",
            "waypoints": [], "km": 5.0, "minutes": 10, "price": price,
        }

    def test_favorite_use_recalculates_after_tariff_change(self):
        """Старая цена в избранном (100) игнорируется, берётся новый тариф."""
        self._set_tariff(500, 100)
        with patch.object(self.maximus, "quote", self._fake_quote):
            res = self.maximus.act(self.user, {"op": "favorite.use", "index": 0})
        self.assertTrue(res["ok"])
        order = res["order"]
        self.assertGreaterEqual(int(order["price"]), 500)
        stored = self.s.get_order(order["id"])
        self.assertGreaterEqual(int(stored["price"]), 500)

    def test_favorite_use_keeps_new_price_when_tariff_unchanged(self):
        with patch.object(self.maximus, "quote", self._fake_quote):
            res = self.maximus.act(self.user, {"op": "favorite.use", "index": 0})
        self.assertTrue(res["ok"])
        # Цена не падает ниже рассчитанной по тарифу (5 км / 10 мин)
        self.assertGreaterEqual(int(res["order"]["price"]),
                                int(self._fake_quote()["price"]))


if __name__ == "__main__":
    unittest.main()
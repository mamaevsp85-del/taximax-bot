"""Своя цена пассажира должна быть больше рассчитанной стоимости.

Проверяем:
  - order.create (Mini-App) отклоняет цену ниже рассчитанной;
  - order.price (смена цены по активному заказу) тоже отклоняет;
  - заказ сохраняет base_price = рассчитанную стоимость.
"""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class OwnPriceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "ownprice.json"),
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
        self.s.ensure_user(20, name="Пассажир", username="p20")
        # profile complete: имя + телефон
        self.s.update_user(20, phone="+79130000000", onboarded=True)
        self._orig = self.maximus.S
        self.maximus.S = self.s

    def tearDown(self):
        self.maximus.S = self._orig

    def _draft(self, calc=110):
        return {
            "pickup": "Абакан, Ленина 1",
            "destination": "Абакан, Мира 5",
            "pickup_coords": {"lat": 53.72, "lon": 91.44},
            "destination_coords": {"lat": 53.73, "lon": 91.45},
            "distance": 3.0, "minutes": 10,
            "price": calc,   # рассчитанная стоимость
        }

    def test_create_below_calculated_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.maximus.act(20, {
                "op": "order.create", "draft": self._draft(110), "price": 90})
        self.assertIn("ниже рассчитанной", str(ctx.exception).lower())

    def test_create_equal_or_above_ok(self):
        r = self.maximus.act(20, {
            "op": "order.create", "draft": self._draft(110), "price": 150})
        self.assertTrue(r["ok"])
        oid = r["order"]["id"]
        order = self.s.get_order(oid)
        self.assertEqual(order["price"], 150)
        self.assertEqual(order["base_price"], 110)

    def test_edit_price_below_base_rejected(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "А", "destination": "Б",
            "price": 110, "base_price": 110, "status": "pending",
        })
        with self.assertRaises(ValueError) as ctx:
            self.maximus.act(20, {"op": "order.price", "id": oid, "price": 60})
        self.assertIn("больше рассчитанной", str(ctx.exception).lower())

    def test_edit_price_equal_base_rejected(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "А", "destination": "Б",
            "price": 110, "base_price": 110, "status": "pending",
        })
        with self.assertRaises(ValueError):
            self.maximus.act(20, {"op": "order.price", "id": oid, "price": 110})

    def test_edit_price_above_base_ok(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "А", "destination": "Б",
            "price": 110, "base_price": 110, "status": "pending",
        })
        r = self.maximus.act(20, {"op": "order.price", "id": oid, "price": 200})
        self.assertTrue(r["ok"])
        self.assertEqual(self.s.get_order(oid)["price"], 200)


if __name__ == "__main__":
    unittest.main()
"""Offline tests: passenger order creation, waypoints, favorites, rating."""
import asyncio
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from types import SimpleNamespace


class PassengerOrderTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "passenger_order.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.passenger = importlib.import_module("handlers.passenger_new")
        cls.driver = importlib.import_module("handlers.driver")
        cls.router_class = importlib.import_module("core.router").Router
        cls.reply_module = importlib.import_module("ui.reply")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.reply_module._MENU_MID.clear()
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.passenger.S = self.s
        self.driver.S = self.s
        self.client = AsyncMock()
        self.client.send_message.return_value = {"message_id": "test-message"}
        self.router = self.router_class(self.client)
        self.passenger.register_passenger(self.router)
        self.driver.register_driver(self.router)
        # driver 10 approved + online
        self.s.ensure_user(10, name="Водитель", username="driver10")
        self.s.update_user(10, driver_verification_status="approved",
                           status="online", car_model="Toyota", car_number="A123BC19")
        # passenger 20
        self.s.ensure_user(20, name="Пассажир", username="pass20")

    def update(self, payload, actor=20):
        return SimpleNamespace(
            callback=SimpleNamespace(payload=payload, callback_id="test"),
            user=SimpleNamespace(user_id=actor),
        )

    def geo_update(self, lat, lon):
        return SimpleNamespace(message=SimpleNamespace(
            location=SimpleNamespace(latitude=lat, longitude=lon)))

    def text_update(self, text):
        return SimpleNamespace(message=SimpleNamespace(text=text, location=None))

    async def test_create_order_and_notify_drivers(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "Абакан, Ленина 1",
            "destination": "Абакан, Мира 5",
            "pickup_coords": {"lat": 53.71, "lon": 91.43},
            "destination_coords": {"lat": 53.72, "lon": 91.44},
            "distance": 3.0, "time_minutes": 10, "price": 150,
            "pickup_city": "Абакан", "destination_city": "Абакан",
            "status": "pending", "created_at": "2024-01-01T00:00:00",
        })
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["pickup_coords"], {"lat": 53.71, "lon": 91.43})
        await self.passenger.notify_drivers_about_order(self.client, oid)
        self.assertTrue(self.client.send_message.called)
        texts = [c.kwargs.get("text", "") for c in self.client.send_message.call_args_list]
        self.assertTrue(any(f"Новый заказ #{oid}" in t for t in texts))

    async def test_notified_driver_not_duplicated(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "Абакан, Ленина 1",
            "destination": "Абакан, Мира 5", "price": 150,
            "status": "pending", "created_at": "2024-01-01T00:00:00",
        })
        self.s.add_notified_driver(oid, 10)
        await self.passenger.notify_drivers_about_order(self.client, oid)
        calls = self.client.send_message.call_args_list
        user_ids = [c.kwargs.get("user_id") for c in calls]
        self.assertNotIn(10, user_ids)

    async def test_waypoints_in_price_calculation(self):
        with patch.object(self.passenger.geocoder, "calculate_distance_and_time",
                          new=AsyncMock(return_value=(8.0, 15))):
            from core.geocoder import calculate_distance_and_time
            wps = [{"lat": 53.715, "lon": 91.435}]
            wp_coords = [(float(w["lat"]), float(w["lon"])) for w in wps
                         if w.get("lat") is not None and w.get("lon") is not None]
            dist, dur = await calculate_distance_and_time(
                53.71, 91.43, 53.72, 91.44, waypoints=wp_coords)
            self.assertEqual(dist, 8.0)
            self.assertEqual(dur, 15)

    async def test_add_and_use_favorite(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "Абакан, Ленина 1",
            "destination": "Абакан, Мира 5", "price": 150,
            "status": "completed", "created_at": "2024-01-01T00:00:00",
        })
        self.s.add_favorite(20, oid, label="Дом → Работа")
        favs = self.s.get_favorites(20)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["label"], "Дом → Работа")
        self.assertTrue(self.s.remove_favorite(20, oid))
        self.assertEqual(len(self.s.get_favorites(20)), 0)
        self.assertFalse(self.s.remove_favorite(20, oid))

    async def test_order_rating(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "А", "destination": "Б",
            "price": 100, "status": "completed",
            "created_at": "2024-01-01T00:00:00",
        })
        self.assertTrue(self.s.set_order_rating(oid, 5, passenger_id=20))
        self.assertEqual(self.s.get_order_rating(oid), 5)
        self.assertFalse(self.s.set_order_rating(oid, 1, passenger_id=99))
        self.assertEqual(self.s.get_order_rating(oid), 5)

    async def test_rate_driver_callback(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "А", "destination": "Б",
            "price": 100, "status": "completed",
            "created_at": "2024-01-01T00:00:00",
        })
        await self.router.callback_handlers["passenger:rate:"](
            self.client, 20, self.update(f"passenger:rate:{oid}:5", 20), {})
        self.assertEqual(self.s.get_order_rating(oid), 5)
        self.assertTrue(self.client.send_message.called)
        texts = [c.kwargs.get("text", "") for c in self.client.send_message.call_args_list]
        self.assertTrue(any("5/5" in t for t in texts))

    async def test_favorite_use_creates_order(self):
        oid = self.s.create_order({
            "passenger_id": 20, "pickup": "Абакан, Ленина 1",
            "destination": "Абакан, Мира 5", "price": 150,
            "status": "completed", "created_at": "2024-01-01T00:00:00",
        })
        self.s.add_favorite(20, oid, label="Дом → Работа")
        with patch.object(self.passenger, "notify_drivers_about_order",
                          new=AsyncMock()):
            await self.router.callback_handlers["passenger:favorite_use:"](
                self.client, 20, self.update("passenger:favorite_use:0", 20), {})
        new_orders = [o for o in self.s.orders.values()
                      if o.get("passenger_id") == 20 and o.get("id") != oid]
        self.assertEqual(len(new_orders), 1)
        self.assertEqual(new_orders[0]["pickup"], "Абакан, Ленина 1")


if __name__ == "__main__":
    unittest.main()
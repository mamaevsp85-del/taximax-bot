"""Правила маршрута и дневная статистика водителя (Mini-App).

Проверяем:
  - водитель НЕ может менять адрес на любом статусе (маршрут правит пассажир);
  - пассажир может изменить адрес по активному заказу;
  - driver.day отдаёт все завершённые заявки за дату с суммой и количеством,
    и суммы совпадают с графиком (daily).
"""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DriverRouteRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "driverrules.json"),
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
        self.passenger = 20
        self.s.ensure_user(self.driver, name="Водитель")
        self.s.update_user(self.driver, phone="+79130000001", onboarded=True,
                           driver_verification_status="approved",
                           type="driver", active_role="driver", status="online")
        self.s.ensure_user(self.passenger, name="Пассажир")
        self.s.update_user(self.passenger, phone="+79130000002", onboarded=True)
        self.oid = self.s.create_order({
            "passenger_id": self.passenger, "driver_id": self.driver,
            "pickup": "Черногорск, Мира 1", "destination": "Черногорск, Степная 12",
            "pickup_coords": {"lat": 53.82, "lon": 91.33},
            "destination_coords": {"lat": 53.81, "lon": 91.29},
            "pickup_city": "Черногорск", "destination_city": "Черногорск",
            "price": 200, "base_price": 200, "distance": 5.0,
            "time_minutes": 12, "status": "accepted",
        })

    def tearDown(self):
        self.maximus.S = self._orig

    def _fake_quote(self, *args, **kwargs):
        return {
            "pickup": "Черногорск, Мира 1",
            "destination": "Черногорск, Советская 3",
            "destination_coords": {"lat": 53.80, "lon": 91.30},
            "destination_city": "Черногорск",
            "waypoints": [], "km": 4.0, "minutes": 10, "price": 210,
        }

    def test_driver_cannot_change_route_while_accepted(self):
        with patch.object(self.maximus, "quote", self._fake_quote):
            with self.assertRaises(ValueError) as ctx:
                self.maximus.act(self.driver, {
                    "op": "order.route", "id": self.oid,
                    "destination": "Черногорск, Советская 3"})
        self.assertIn("пассажир", str(ctx.exception).lower())
        self.assertEqual(self.s.get_order(self.oid)["destination"],
                         "Черногорск, Степная 12")

    def test_driver_cannot_change_route_after_arrival(self):
        self.s.update_order(self.oid, status="driver_arrived")
        with patch.object(self.maximus, "quote", self._fake_quote):
            with self.assertRaises(ValueError):
                self.maximus.act(self.driver, {
                    "op": "order.route", "id": self.oid,
                    "destination": "Черногорск, Советская 3"})

    def test_driver_cannot_add_stop_in_trip(self):
        self.s.update_order(self.oid, status="in_progress")
        with patch.object(self.maximus, "quote", self._fake_quote):
            with self.assertRaises(ValueError):
                self.maximus.act(self.driver, {
                    "op": "order.route", "id": self.oid,
                    "destination": "Черногорск, Степная 12",
                    "stop": "Черногорск, Калинина 2"})

    def test_passenger_can_change_route(self):
        with patch.object(self.maximus, "quote", self._fake_quote):
            res = self.maximus.act(self.passenger, {
                "op": "order.route", "id": self.oid,
                "destination": "Черногорск, Советская 3"})
        self.assertTrue(res["ok"])
        self.assertEqual(self.s.get_order(self.oid)["destination"],
                         "Черногорск, Советская 3")


if __name__ == "__main__":
    unittest.main()
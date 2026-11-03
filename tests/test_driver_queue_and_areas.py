"""Два заказа водителя, районы города и межгород, деньги в деталях заявки."""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DriverQueueAndAreasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "queue.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.support = importlib.import_module("core.driver_support")
        cls.areas = importlib.import_module("core.areas")
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
        self.driver = 10
        self.s.ensure_user(self.driver, name="Водитель")
        self.s.update_user(self.driver, phone="+79130000001", onboarded=True,
                           driver_verification_status="approved",
                           type="driver", active_role="driver", status="online")
        self.s.ensure_user(20, name="Пассажир")
        self.s.ensure_user(21, name="Пассажир 2")

    def tearDown(self):
        self.maximus.S = self._orig
        self.support.S = self._orig_support

    def _order(self, passenger_id=20, pickup="Черногорск, Мира 1",
               destination="Черногорск, Степная 12", pickup_city="Черногорск",
               destination_city="Черногорск", lat=53.82, lon=91.29, price=300):
        return self.s.create_order({
            "passenger_id": passenger_id,
            "pickup": pickup, "destination": destination,
            "pickup_city": pickup_city, "destination_city": destination_city,
            "pickup_coords": {"lat": lat, "lon": lon},
            "destination_coords": {"lat": lat + 0.01, "lon": lon + 0.01},
            "price": price, "base_price": price, "distance": 5.0,
            "time_minutes": 12, "status": "pending",
        })

    def _accept(self, oid):
        return self.maximus.accept(self.driver, oid)

    # ---------- второй заказ ----------
    def test_second_order_blocked_until_trip_started(self):
        oid1 = self._order()
        oid2 = self._order(passenger_id=21, pickup="Черногорск, Крепость 5")
        self._accept(oid1)
        with self.assertRaises(ValueError):
            self._accept(oid2)
        # Начали движение по первому — второй брать можно.
        self.s.update_order(oid1, status="in_progress")
        self._accept(oid2)
        self.assertEqual(self.s.get_order(oid2)["driver_id"], self.driver)

    def test_only_two_orders_max(self):
        oid1 = self._order()
        oid2 = self._order(passenger_id=21)
        oid3 = self._order(passenger_id=22)
        self._accept(oid1)
        self.s.update_order(oid1, status="in_progress")
        self._accept(oid2)
        with self.assertRaises(ValueError):
            self._accept(oid3)

    def test_next_order_cannot_be_managed(self):
        oid1 = self._order()
        oid2 = self._order(passenger_id=21)
        self._accept(oid1)
        self.s.update_order(oid1, status="in_progress")
        self._accept(oid2)
        active = self.s.get_driver_active_orders(self.driver)
        self.assertEqual([o["id"] for o in active], [oid1, oid2])
        with self.assertRaises(ValueError):
            self.maximus.own_current_order(self.driver, oid2)
        self.assertEqual(self.maximus.own_current_order(self.driver, oid1)["id"], oid1)

    def test_current_order_id_stays_on_first(self):
        oid1 = self._order()
        oid2 = self._order(passenger_id=21)
        self._accept(oid1)
        self.s.update_order(oid1, status="in_progress")
        self._accept(oid2)
        self.assertEqual(self.s.get_user(self.driver).get("driver_current_order_id"), oid1)
        self.assertEqual(self.s.get_driver_next_order(self.driver)["id"], oid2)

    # ---------- сортировка от точки Б ----------
    def test_reference_point_is_current_trip_destination(self):
        oid1 = self._order(lat=53.80, lon=91.30)
        self._accept(oid1)
        self.s.update_order(oid1, status="in_progress",
                            destination_coords={"lat": 53.90, "lon": 91.40})
        ref = self.s.get_driver_reference_point(self.driver)
        self.assertEqual(ref["source"], "trip")
        self.assertAlmostEqual(ref["lat"], 53.90)

    def test_reference_point_falls_back_to_last_order(self):
        oid1 = self._order()
        self._accept(oid1)
        self.s.update_order(oid1, status="completed",
                            completed_at="2026-01-01T10:00:00",
                            destination_coords={"lat": 53.75, "lon": 91.25})
        ref = self.s.get_driver_reference_point(self.driver)
        self.assertEqual(ref["source"], "last_order")
        self.assertAlmostEqual(ref["lat"], 53.75)

    def test_available_sorted_nearest_first(self):
        self.s.update_user(self.driver, last_lat=53.80, last_lon=91.30)
        near = self._order(pickup="Черногорск, близко", lat=53.801, lon=91.301)
        far = self._order(passenger_id=21, pickup="Абакан, далеко",
                          pickup_city="Абакан", lat=53.72, lon=91.42)
        # get_available_orders_for_driver работает с глобальным хранилищем.
        original = self.storage_module.storage
        self.storage_module.storage = self.s
        try:
            rows = self.storage_module.get_available_orders_for_driver(self.driver)
        finally:
            self.storage_module.storage = original
        self.assertEqual([o["id"] for o in rows], [near, far])

    # ---------- районы и межгород ----------
    def test_districts_detected(self):
        self.assertEqual(self.areas.detect_district("Крепость, 12", "Черногорск"), "Крепость")
        self.assertEqual(self.areas.detect_district("9-й Трест, 4", "Черногорск"), "Девятый Трест")
        self.assertEqual(self.areas.detect_district("Полярка, 5", "Абакан"), "Полярка")
        self.assertEqual(self.areas.detect_district("Автовокзал", "Абакан"), "Автовокзал")
        self.assertEqual(self.areas.detect_district("Северный, 3", "Абакан"), "Северный")

    def test_groups_split_districts_and_intercity(self):
        rows = [
            {"pickup": "Крепость 5", "pickup_city": "Черногорск",
             "destination_city": "Черногорск", "pickup_coords": {"lat": 53.83, "lon": 91.31}},
            {"pickup": "Крепость 9", "pickup_city": "Черногорск",
             "destination_city": "Черногорск", "pickup_coords": {"lat": 53.84, "lon": 91.32}},
            {"pickup": "Полярка 3", "pickup_city": "Абакан",
             "destination_city": "Абакан", "pickup_coords": {"lat": 53.72, "lon": 91.42}},
            {"pickup": "Мира 1", "pickup_city": "Черногорск",
             "destination_city": "Минусинск", "pickup_coords": {"lat": 53.82, "lon": 91.29}},
        ]
        groups = self.areas.group_orders_by_area(rows, 53.80, 91.30)
        titles = [g["title"] for g in groups]
        self.assertIn("Крепость · Черногорск", titles)
        self.assertIn("Полярка · Абакан", titles)
        self.assertIn("Межгород · Минусинск", titles)
        krepost = [g for g in groups if g["title"].startswith("Крепость")][0]
        self.assertEqual(krepost["count"], 2)
        self.assertEqual(krepost["orders"][0]["pickup"], "Крепость 5")

    def test_settlements_catalog(self):
        self.assertIn("Черногорск", self.areas.KHAKASSIA_SETTLEMENTS)
        self.assertIn("Абакан", self.areas.KHAKASSIA_SETTLEMENTS)
        self.assertIn("Минусинск", self.areas.SOUTH_KRAI_SETTLEMENTS)
        self.assertIn("Шушенское", self.areas.SOUTH_KRAI_SETTLEMENTS)

    # ---------- деньги ----------
    def test_brief_order_money_without_commission(self):
        oid = self._order(price=500)
        self.s.update_order(oid, status="completed", final_price=500)
        brief = self.maximus.brief_order(self.s.get_order(oid))
        self.assertAlmostEqual(brief["total_price"], 500)
        self.assertEqual(brief["commission_amount"], 0)
        self.assertAlmostEqual(brief["driver_earning"], 500)

    def test_brief_order_money_with_commission(self):
        oid = self._order(price=1000)
        self.s.update_order(oid, status="completed", final_price=1000,
                            waiting_cost=0, driver_earning=900)
        cfg = self.s.get_commission_config()
        cfg["enabled"] = True
        cfg["mode"] = "percent"
        cfg["percent"] = 10
        self.s.admin_data["driver_commission"] = cfg
        brief = self.maximus.brief_order(self.s.get_order(oid))
        self.assertAlmostEqual(brief["driver_earning"], 900)
        self.assertAlmostEqual(brief["commission_amount"], 100)
        self.assertEqual(brief["commission_percent"], 10)

    def test_day_returns_earning_total(self):
        oid = self._order(price=700)
        self.s.update_order(oid, driver_id=self.driver, status="completed",
                            completed_at="2026-02-02T12:00:00",
                            final_price=700, driver_earning=630)
        res = self.maximus.act(self.driver, {"op": "driver.day", "date": "2026-02-02"})
        self.assertEqual(res["count"], 1)
        self.assertAlmostEqual(res["earning"], 630)
        self.assertAlmostEqual(res["sum"], 700)

    def test_brief_order_hides_passenger_phone(self):
        oid = self._order()
        self.s.update_user(20, phone="+79130000099")
        # В работе — телефон доступен (нужен для связи).
        self.s.update_order(oid, driver_id=self.driver, status="in_progress")
        brief = self.maximus.brief_order(self.s.get_order(oid))
        self.assertEqual(brief["passenger_phone"], "+79130000099")
        self.assertTrue(brief["can_write"])
        # Закрытая заявка — телефона нет, остаётся кнопка «Написать».
        self.s.update_order(oid, status="completed",
                            completed_at="2026-02-03T12:00:00",
                            final_price=300, driver_earning=300)
        brief = self.maximus.brief_order(self.s.get_order(oid))
        self.assertEqual(brief["passenger_phone"], "")
        self.assertTrue(brief["can_write"])
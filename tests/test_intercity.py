"""Межгород — когда город подачи и город назначения разные."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class IntercityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "ic.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.tariff = importlib.import_module("core.tariff_manager")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    # ---------- _is_intercity ----------
    def test_same_city_is_not_intercity(self):
        self.assertFalse(self.tariff.is_intercity("Черногорск", "Черногорск"))

    def test_different_cities_is_intercity(self):
        self.assertTrue(self.tariff.is_intercity("Черногорск", "Абакан"))

    def test_missing_destination_city_is_not_intercity(self):
        self.assertFalse(self.tariff.is_intercity("Черногорск", ""))
        self.assertFalse(self.tariff.is_intercity("Черногорск", None))

    def test_missing_pickup_city_is_not_intercity(self):
        self.assertFalse(self.tariff.is_intercity("", "Абакан"))

    # ---------- resolve_destination_city ----------
    def test_different_cities_kept_even_without_city_in_text(self):
        """Разные города — межгород, даже если пассажир не написал название."""
        city = self.tariff.resolve_destination_city(
            "Черногорск", "Абакан", "Юбилейная 29")
        self.assertEqual(city, "Абакан")

    def test_far_mismatch_without_city_name_stays_local(self):
        """Улица нашлась за тысячи км и город не назван — это не межгород."""
        city = self.tariff.resolve_destination_city(
            "Черногорск", "Москва", "Юбилейная 29", distance_km=4200)
        self.assertEqual(city, "Черногорск")

    def test_nearby_different_city_is_intercity(self):
        """Соседний город в паре десятков км — межгород."""
        city = self.tariff.resolve_destination_city(
            "Черногорск", "Абакан", "Аскизская 212", distance_km=18)
        self.assertEqual(city, "Абакан")

    def test_explicit_other_city_kept(self):
        """Пассажир явно написал «Абакан» — межгород остаётся."""
        city = self.tariff.resolve_destination_city(
            "Черногорск", "Абакан", "Абакан, Аскизская 212")
        self.assertEqual(city, "Абакан")

    def test_unknown_destination_city_falls_back_to_pickup(self):
        city = self.tariff.resolve_destination_city("Черногорск", "", "Ленина 5")
        self.assertEqual(city, "Черногорск")

    def test_same_city_preserved(self):
        city = self.tariff.resolve_destination_city(
            "Черногорск", "Черногорск", "Ленина 5")
        self.assertEqual(city, "Черногорск")

    def test_no_pickup_city_returns_destination(self):
        city = self.tariff.resolve_destination_city("", "Абакан", "Абакан")
        self.assertEqual(city, "Абакан")

    # ---------- цена ----------
    def test_price_within_city_uses_local_tariff(self):
        """Поездка внутри города — цена как за городскую."""
        price = self.tariff.calculate_trip_price(
            5.0, 10, pickup_city="Черногорск", destination_city="Черногорск")
        self.assertGreater(price, 0)
        self.assertLess(price, 1000)

    def test_price_intercity_differs(self):
        """Межгород считается по другому тарифу."""
        local = self.tariff.calculate_trip_price(
            20.0, 30, pickup_city="Черногорск", destination_city="Черногорск")
        inter = self.tariff.calculate_trip_price(
            20.0, 30, pickup_city="Черногорск", destination_city="Абакан")
        self.assertNotEqual(local, inter)

    def test_scenario_different_cities_use_intercity(self):
        """Город подачи и город назначения разные — межгород."""
        pickup_city = "Черногорск"
        geocoded_city = "Абакан"
        dest_text = "Степная 128А"

        dest_city = self.tariff.resolve_destination_city(
            pickup_city, geocoded_city, dest_text, distance_km=16)
        self.assertEqual(dest_city, "Абакан")
        self.assertTrue(self.tariff.is_intercity(pickup_city, dest_city))

    def test_scenario_city_a_to_explicit_city_b(self):
        """Пассажир из А в Б с явным городом — межгород."""
        pickup_city = "Черногорск"
        geocoded_city = "Абакан"
        dest_text = "Абакан, Аскизская 212"

        dest_city = self.tariff.resolve_destination_city(
            pickup_city, geocoded_city, dest_text)
        self.assertEqual(dest_city, "Абакан")
        self.assertTrue(self.tariff.is_intercity(pickup_city, dest_city))

    def test_city_tariff_found_regardless_of_letter_case(self):
        self.tariff.storage.storage.admin_data["tariffs_by_city"] = {
            "Абакан": {"price_per_km": 55, "base_price": 80},
        }
        tariff = self.tariff.get_tariff_settings(city="абакан")
        self.assertEqual(tariff["price_per_km"], 55)
        self.assertEqual(tariff["base_price"], 80)

    def test_admin_can_edit_and_delete_city_tariff(self):
        import maximus_server as srv
        # ВАЖНО: tariff_manager читает из core.storage.storage, а admin_act
        # пишет в maximus_server.S. Если это разные объекты (api_server при
        # импорте пересоздаёт storage.storage), значения расходятся.
        # Поэтому подменяем оба на одну базу.
        orig_srv = srv.S
        orig_storage = self.tariff.storage.storage
        test_db = self.tariff.storage.Storage(
            str(Path(self.temp.name) / "tariff_admin.json"))
        srv.S = test_db
        self.tariff.storage.storage = test_db
        try:
            srv.S.admin_data["tariffs_by_city"] = {}
            saved = srv.admin_act({
                "op": "admin.tariff", "city": "Абакан",
                "price_per_km": 40, "base_price": 120,
            })
            self.assertTrue(saved["ok"])
            edited = srv.admin_act({
                "op": "admin.tariff", "city": "абакан", "min_price": 90,
            })
            self.assertTrue(edited["ok"])
            tariff = self.tariff.get_tariff_settings(city="Абакан")
            self.assertEqual(tariff["price_per_km"], 40)
            self.assertEqual(tariff["base_price"], 120)
            self.assertEqual(tariff["min_price"], 90)
            srv.admin_act({"op": "admin.tariff.delete", "city": "Абакан"})
            after = self.tariff.get_tariff_settings(city="абакан")
            self.assertEqual(after["price_per_km"], 20)
            self.assertEqual(after["base_price"], 100)
        finally:
            srv.S = orig_srv
            self.tariff.storage.storage = orig_storage


if __name__ == "__main__":
    unittest.main()

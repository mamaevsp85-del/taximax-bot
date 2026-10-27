"""Тесты логики межгорода: не должен срабатывать без явного указания города."""
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
    def test_geocoder_wrong_city_ignored_when_not_in_text(self):
        """Геокодер нашёл улицу в Абакане, но пассажир город не писал."""
        city = self.tariff.resolve_destination_city(
            "Черногорск", "Абакан", "Юбилейная 29")
        self.assertEqual(city, "Черногорск")

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

    def test_scenario_city_a_to_city_a_same_address(self):
        """Пассажир из города А в город А — не межгород, даже если улица нашлась в Б."""
        pickup_city = "Черногорск"
        geocoded_city = "Абакан"     # геокодер ошибся
        dest_text = "Степная 128А"    # город не указан

        dest_city = self.tariff.resolve_destination_city(
            pickup_city, geocoded_city, dest_text)
        self.assertEqual(dest_city, pickup_city)
        self.assertFalse(self.tariff.is_intercity(pickup_city, dest_city))

    def test_scenario_city_a_to_explicit_city_b(self):
        """Пассажир из А в Б с явным городом — межгород."""
        pickup_city = "Черногорск"
        geocoded_city = "Абакан"
        dest_text = "Абакан, Аскизская 212"

        dest_city = self.tariff.resolve_destination_city(
            pickup_city, geocoded_city, dest_text)
        self.assertEqual(dest_city, "Абакан")
        self.assertTrue(self.tariff.is_intercity(pickup_city, dest_city))


if __name__ == "__main__":
    unittest.main()

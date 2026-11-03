"""Точность адресов и остановки в расчёте заказа (серверная логика)."""
import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class QuoteAccuracyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "quote.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.maximus = importlib.import_module("maximus_server")
        cls.areas = importlib.import_module("core.areas")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    # ---------- остановки ----------
    def test_stops_list_from_list_and_single(self):
        fn = self.maximus._stops_list
        self.assertEqual(fn("", ["А", "Б"]), ["А", "Б"])
        self.assertEqual(fn("Одна", None), ["Одна"])
        self.assertEqual(fn("", "Одна"), ["Одна"])
        # дубли и пустые — убираются
        self.assertEqual(fn("А", ["А", " ", "Б"]), ["А", "Б"])

    def test_stops_list_keeps_order(self):
        self.assertEqual(
            self.maximus._stops_list("", ["Первая", "Вторая", "Третья"]),
            ["Первая", "Вторая", "Третья"])

    # ---------- привязка к городу ----------
    def test_city_mentioned(self):
        fn = self.maximus._city_mentioned
        self.assertTrue(fn("Черногорск, Мира 1", "Черногорск"))
        self.assertTrue(fn("черногорск мира 1", "Черногорск"))
        self.assertFalse(fn("Мира 1", "Черногорск"))

    def test_explicit_other_city_detected(self):
        fn = self.maximus._has_explicit_other_city
        self.assertTrue(fn("Минусинск, Ленина 20", "Черногорск"))
        self.assertTrue(fn("Абакан, Ленина 20", "Черногорск"))
        self.assertFalse(fn("Ленина 20", "Черногорск"))
        # Свой город — не «чужой»
        self.assertFalse(fn("Черногорск, Ленина 20", "Черногорск"))

    def test_known_cities_cover_neighbours(self):
        # Соседние города и посёлки должны распознаваться как «явно названные».
        for city in ("абакан", "минусинск", "шушенское", "саяногорск", "сорск"):
            self.assertIn(city, self.maximus.KNOWN_CITIES)

    def test_geo_checked_rejects_city_only_point(self):
        """Если геокодер вернул «просто город» вместо улицы — адрес не найден."""
        async def fake_precise(address):
            return {"lat": 53.82, "lon": 91.29}, "Черногорск", "other"

        async def fake_in_city(address, city=""):
            return {"lat": 53.82, "lon": 91.29}, "Черногорск"

        with patch.object(self.maximus.geocoder, "geocode_precise", fake_precise), \
             patch.object(self.maximus, "geo_in_city", fake_in_city):
            coords, city = asyncio.run(
                self.maximus.geo_checked("ул. Несуществующая 9999", "Черногорск"))
        self.assertIsNone(coords)

    def test_geo_checked_accepts_street_precision(self):
        async def fake_precise(address):
            return {"lat": 53.82, "lon": 91.29}, "Черногорск", "street"

        async def fake_in_city(address, city=""):
            return {"lat": 53.82, "lon": 91.29}, "Черногорск"

        with patch.object(self.maximus.geocoder, "geocode_precise", fake_precise), \
             patch.object(self.maximus, "geo_in_city", fake_in_city):
            coords, _city = asyncio.run(
                self.maximus.geo_checked("Мира 1", "Черногорск"))
        self.assertIsNotNone(coords)


if __name__ == "__main__":
    unittest.main()
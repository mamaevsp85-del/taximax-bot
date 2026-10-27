"""Тесты формирования короткого адреса."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ShortAddressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "addr.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.geocoder = importlib.import_module("core.geocoder")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def test_city_street_house_only(self):
        comps = [
            {"kind": "country", "name": "Россия"},
            {"kind": "province", "name": "Республика Хакасия"},
            {"kind": "area", "name": "Усть-Абаканский район"},
            {"kind": "locality", "name": "Черногорск"},
            {"kind": "street", "name": "Степная улица"},
            {"kind": "house", "name": "128А"},
        ]
        self.assertEqual(self.geocoder.short_address(comps),
                         "Черногорск, Степная улица, 128А")

    def test_locality_beats_area(self):
        """Район не должен подменять город."""
        comps = [
            {"kind": "area", "name": "Усть-Абаканский район"},
            {"kind": "locality", "name": "Черногорск"},
            {"kind": "street", "name": "Юбилейная улица"},
            {"kind": "house", "name": "29"},
        ]
        self.assertEqual(self.geocoder.short_address(comps),
                         "Черногорск, Юбилейная улица, 29")

    def test_no_country_or_region(self):
        comps = [
            {"kind": "country", "name": "Россия"},
            {"kind": "province", "name": "Республика Хакасия"},
            {"kind": "locality", "name": "Абакан"},
            {"kind": "street", "name": "Аскизская улица"},
            {"kind": "house", "name": "212"},
        ]
        result = self.geocoder.short_address(comps)
        self.assertNotIn("Россия", result)
        self.assertNotIn("Хакасия", result)
        self.assertEqual(result, "Абакан, Аскизская улица, 212")

    def test_house_without_street(self):
        """Посёлок без улиц — остаётся населённый пункт и дом."""
        comps = [
            {"kind": "locality", "name": "Копьёво"},
            {"kind": "house", "name": "12"},
        ]
        self.assertEqual(self.geocoder.short_address(comps), "Копьёво, 12")

    def test_no_house_falls_back_to_street(self):
        comps = [
            {"kind": "locality", "name": "Черногорск"},
            {"kind": "street", "name": "Степная улица"},
        ]
        self.assertEqual(self.geocoder.short_address(comps),
                         "Черногорск, Степная улица")

    def test_empty_components(self):
        self.assertEqual(self.geocoder.short_address([]), "")
        self.assertEqual(self.geocoder.short_address(None), "")


if __name__ == "__main__":
    unittest.main()

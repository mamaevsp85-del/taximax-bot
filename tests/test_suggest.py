"""Тесты автодополнения адреса."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


class SuggestTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "suggest.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.geocoder = importlib.import_module("core.geocoder")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    async def test_short_query_returns_empty(self):
        self.assertEqual(await self.geocoder.suggest_address("а"), [])
        self.assertEqual(await self.geocoder.suggest_address(""), [])
        self.assertEqual(await self.geocoder.suggest_address("  "), [])

    async def test_returns_houses_for_known_street(self):
        items = await self.geocoder.suggest_address("степная", city="Черногорск", limit=5)
        self.assertTrue(items, "нет подсказок для 'степная' в Черногорске")
        for it in items:
            self.assertIn("Степная", it["title"])
            self.assertIn("Черногорск", it["subtitle"])
            self.assertIn(it["kind"], ("street", "house"))

    async def test_city_filter_excludes_other_regions(self):
        """Без города 'степная 128' даёт чужие регионы, с городом — только свой."""
        items = await self.geocoder.suggest_address("степная 128", city="Черногорск", limit=5)
        self.assertTrue(items)
        for it in items:
            self.assertIn("Черногорск", it["subtitle"])

    async def test_locality_items_filtered_out(self):
        """Сам город не должен попадать в подсказки адреса."""
        items = await self.geocoder.suggest_address("черногорск", city="Черногорск", limit=5)
        for it in items:
            self.assertNotEqual(it["kind"], "locality")
            self.assertNotEqual(it["kind"], "province")

    async def test_result_shape(self):
        items = await self.geocoder.suggest_address("юбилейная", city="Черногорск", limit=3)
        self.assertTrue(items)
        first = items[0]
        for key in ("title", "subtitle", "address", "kind"):
            self.assertIn(key, first)


if __name__ == "__main__":
    unittest.main()

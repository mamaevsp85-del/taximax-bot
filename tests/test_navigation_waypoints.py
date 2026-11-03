"""Тесты: навигация с остановками, наценки, подсказки по близости."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class NavWithWaypointsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "nav.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.support = importlib.import_module("core.driver_support")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def test_waypoints_included_in_navigation(self):
        """Навигатор должен вести А → остановка → Б, а не сразу А → Б."""
        order = {
            "pickup_coords": {"lat": 53.81, "lon": 91.30},
            "destination_coords": {"lat": 53.73, "lon": 91.45},
            "waypoints": [{"text": "Калинина 2", "lat": 53.82, "lon": 91.33}],
        }
        url = self.support.generate_navigation_url(order)
        self.assertIn("53.82,91.33", url)
        self.assertEqual(url.count("~"), 2)  # три точки

    def test_latitude_longitude_format_supported(self):
        """Поддерживается и старый формат latitude/longitude."""
        order = {
            "pickup_coords": {"lat": 53.81, "lon": 91.30},
            "destination_coords": {"lat": 53.73, "lon": 91.45},
            "waypoints": [{"text": "X", "latitude": 53.82, "longitude": 91.33}],
        }
        url = self.support.generate_navigation_url(order)
        self.assertIn("53.82,91.33", url)

    def test_no_waypoints_simple_route(self):
        order = {"pickup_coords": {"lat": 53.81, "lon": 91.30},
                 "destination_coords": {"lat": 53.73, "lon": 91.45},
                 "waypoints": []}
        url = self.support.generate_navigation_url(order)
        self.assertEqual(url.count("~"), 1)

    def test_empty_order_returns_maps(self):
        self.assertEqual(self.support.generate_navigation_url({}),
                         "https://yandex.ru/maps")


if __name__ == "__main__":
    unittest.main()
"""Тесты кэшей и устойчивости геокодера (без реальной сети)."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _suggest_payload(items):
    return {"results": items}


def _item(title, subtitle, dist_m, tag="house"):
    return {
        "title": {"text": title},
        "subtitle": {"text": subtitle},
        "address": {"formatted_address": f"{title}, {subtitle}"},
        "tags": [tag],
        "distance": {"value": dist_m},
    }


class GeocoderCacheTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "geo.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "YANDEX_API_KEY": "test-key",
        })
        cls.env.start()
        cls.geocoder = importlib.import_module("core.geocoder")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.geocoder._SUGGEST_CACHE.clear()
        self.geocoder._GEOCODE_CACHE.clear()
        self.geocoder._CITY_CENTER.clear()

    async def test_suggest_puts_own_city_first(self):
        """Сначала адреса родного города (по возрастанию расстояния), затем
        ближайшие населённые пункты — тоже по возрастанию расстояния."""
        payload = _suggest_payload([
            _item("улица Мира, 1", "Владимир, Владимирская область", 100),
            _item("улица Мира, 1а", "Черногорск, Республика Хакасия", 3000),
            _item("улица Мира, 1б", "Черногорск, Республика Хакасия", 1000),
        ])

        async def fake(url, params, attempts=2):
            return payload

        with patch.object(self.geocoder, "_fetch_json", fake), \
             patch.object(self.geocoder, "city_center",
                          lambda city: _async_value({"lat": 53.8, "lon": 91.3})):
            items = await self.geocoder.suggest_address(
                "Мира 1 сортировка", city="Черногорск", limit=5)
        self.assertEqual(len(items), 3)
        # Свой город — первым, внутри своего города — ближний выше.
        self.assertIn("Черногорск", items[0]["subtitle"])
        self.assertIn("Черногорск", items[1]["subtitle"])
        self.assertLessEqual(items[0]["distance_km"], items[1]["distance_km"])
        # Чужой регион не выбрасываем, но он идёт строго последним.
        self.assertIn("Владимир", items[2]["subtitle"])

    async def test_suggest_cached_second_call_no_request(self):
        calls = {"n": 0}
        payload = _suggest_payload([
            _item("улица Пушкина, 22", "Черногорск, Республика Хакасия", 700),
        ])

        async def fake(url, params, attempts=2):
            calls["n"] += 1
            return payload

        with patch.object(self.geocoder, "_fetch_json", fake), \
             patch.object(self.geocoder, "city_center", lambda city: _async_value(None)):
            first = await self.geocoder.suggest_address(
                "Пушкина кэш", city="Черногорск", limit=5)
            second = await self.geocoder.suggest_address(
                "Пушкина кэш", city="Черногорск", limit=5)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)

    async def test_suggest_network_error_returns_empty(self):
        async def boom(url, params, attempts=2):
            raise RuntimeError("network down")

        with patch.object(self.geocoder, "_fetch_json", boom):
            items = await self.geocoder.suggest_address(
                "нет сети вообще", city="Черногорск", limit=5)
        self.assertEqual(items, [])

    async def test_suggest_without_city_keeps_all(self):
        payload = _suggest_payload([
            _item("улица Мира, 15", "Черногорск, Республика Хакасия", 100),
            _item("улица Мира, 7", "Абакан, Республика Хакасия", 900),
        ])

        async def fake(url, params, attempts=2):
            return payload

        with patch.object(self.geocoder, "_fetch_json", fake):
            items = await self.geocoder.suggest_address("Мира все", limit=5)
        self.assertEqual(len(items), 2)
        self.assertNotIn("in_city", items[0])
        # Без города порядок — по расстоянию от точки отсчёта.
        self.assertLessEqual(items[0]["distance_km"], items[1]["distance_km"])

    async def test_concurrent_requests_share_one_api_call(self):
        """50 пассажиров с одинаковым запросом = 1 запрос к Яндексу."""
        calls = {"n": 0}
        payload = _suggest_payload([
            _item("улица Мира, 38", "Черногорск, Республика Хакасия", 100),
        ])

        async def fake(url, params, attempts=2):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return payload

        with patch.object(self.geocoder, "_fetch_json", fake), \
             patch.object(self.geocoder, "city_center",
                          lambda city: _async_value({"lat": 53.8, "lon": 91.3})):
            results = await asyncio.gather(*[
                self.geocoder.suggest_address("Мира параллель", city="Черногорск",
                                              limit=5)
                for _ in range(50)
            ])
        self.assertEqual(calls["n"], 1)
        for r in results:
            self.assertEqual(len(r), 1)

    async def test_near_point_used_as_reference(self):
        """Если передана точка пассажира — ll берётся из неё, без геокодинга."""
        seen = {}

        async def fake(url, params, attempts=2):
            seen.update(params)
            return _suggest_payload([
                _item("улица Мира, 38", "Черногорск, Республика Хакасия", 100),
            ])

        async def boom(city):
            raise AssertionError("city_center не должен вызываться")

        with patch.object(self.geocoder, "_fetch_json", fake), \
             patch.object(self.geocoder, "city_center", boom):
            items = await self.geocoder.suggest_address(
                "Мира рядом", city="Черногорск", limit=5,
                near_lat=53.81, near_lon=91.32)
        self.assertTrue(items)
        self.assertEqual(seen.get("ll"), "91.32,53.81")


class DistanceCalcTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.geocoder = importlib.import_module("core.geocoder")

    async def test_returns_tuple_when_routers_unavailable(self):
        """Раньше функция молча возвращала None — ломала избранное и заказы."""
        class _BoomClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                raise RuntimeError("no network")

        with patch.object(self.geocoder.httpx, "AsyncClient", _BoomClient):
            result = await self.geocoder.calculate_distance_and_time(
                53.81, 91.30, 53.73, 91.45)
        self.assertIsNotNone(result)
        dist, dur = result
        self.assertGreater(dist, 0)
        self.assertGreaterEqual(dur, 1)

    async def test_waypoints_increase_distance(self):
        class _BoomClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                raise RuntimeError("no network")

        with patch.object(self.geocoder.httpx, "AsyncClient", _BoomClient):
            direct = await self.geocoder.calculate_distance_and_time(
                53.81, 91.30, 53.73, 91.45)
            via = await self.geocoder.calculate_distance_and_time(
                53.81, 91.30, 53.73, 91.45,
                waypoints=[(53.90, 91.60)])
        self.assertGreater(via[0], direct[0])

    async def test_bad_coordinates_return_none(self):
        dist, dur = await self.geocoder.calculate_distance_and_time(
            999, 999, 53.73, 91.45)
        self.assertIsNone(dist)
        self.assertIsNone(dur)


def _async_value(value):
    async def _coro():
        return value
    return _coro()


if __name__ == "__main__":
    unittest.main()
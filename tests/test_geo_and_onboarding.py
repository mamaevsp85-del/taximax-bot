"""Тесты гео-запросов через бота и одноразового знакомства."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class GeoRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "geo.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.s.ensure_user(10)

    def test_request_created_pending(self):
        self.s.set_geo_request(10, kind="pickup")
        self.assertTrue(self.s.has_pending_geo_request(10))
        req = self.s.get_geo_request(10)
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["kind"], "pickup")

    def test_complete_request(self):
        self.s.set_geo_request(10, kind="pickup")
        self.s.complete_geo_request(10, 53.81, 91.30, "Черногорск, Степная 1", "Черногорск")
        req = self.s.get_geo_request(10)
        self.assertEqual(req["status"], "done")
        self.assertAlmostEqual(req["lat"], 53.81)
        self.assertAlmostEqual(req["lon"], 91.30)
        self.assertEqual(req["address"], "Черногорск, Степная 1")
        self.assertFalse(self.s.has_pending_geo_request(10))

    def test_cancel_request(self):
        self.s.set_geo_request(10)
        self.s.clear_geo_request(10)
        self.assertIsNone(self.s.get_geo_request(10))

    def test_no_request_returns_none(self):
        self.assertIsNone(self.s.get_geo_request(10))
        self.assertFalse(self.s.has_pending_geo_request(10))

    def test_requests_isolated_between_users(self):
        self.s.ensure_user(11)
        self.s.set_geo_request(10)
        self.assertTrue(self.s.has_pending_geo_request(10))
        self.assertFalse(self.s.has_pending_geo_request(11))


class OnboardingOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "onb.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.srv = importlib.import_module("maximus_server")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.srv.S = self.s

    def test_new_user_needs_onboarding(self):
        self.s.ensure_user(10)
        self.assertTrue(self.srv._needs_onboarding(self.s.get_user(10)))

    def test_after_profile_needs_onboarding_false(self):
        self.s.ensure_user(10)
        self.s.update_user(10, name="Иван", phone="79134430404", home_city="Черногорск")
        self.s.mark_onboarded(10)
        u = self.s.get_user(10)
        self.assertFalse(self.srv._needs_onboarding(u))
        self.assertTrue(self.srv._profile_complete(u))

    def test_onboarded_stays_false_even_if_field_cleared(self):
        """Прошёл знакомство — больше не спрашиваем, даже если поле пустое."""
        self.s.ensure_user(10)
        self.s.update_user(10, name="Иван", phone="79134430404", home_city="Черногорск")
        self.s.mark_onboarded(10)
        self.s.update_user(10, home_city="")
        u = self.s.get_user(10)
        self.assertFalse(self.srv._needs_onboarding(u))
        self.assertFalse(self.srv._profile_complete(u))

    def test_user_card_reports_needs_onboarding(self):
        self.s.ensure_user(10)
        card = self.srv.user_card(10)
        self.assertTrue(card["needs_onboarding"])
        self.assertFalse(card["onboarded"])
        self.s.update_user(10, name="Иван", phone="79134430404", home_city="Черногорск")
        self.s.mark_onboarded(10)
        card = self.srv.user_card(10)
        self.assertFalse(card["needs_onboarding"])
        self.assertTrue(card["onboarded"])


if __name__ == "__main__":
    unittest.main()

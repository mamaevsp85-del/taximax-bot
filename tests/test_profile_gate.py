"""Тесты обязательного профиля перед заказом."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ProfileGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "gate.json"),
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

    def test_empty_profile_incomplete(self):
        self.s.ensure_user(10)
        self.assertFalse(self.srv._profile_complete(self.s.get_user(10)))

    def test_only_name_incomplete(self):
        self.s.ensure_user(10, name="Иван")
        self.assertFalse(self.srv._profile_complete(self.s.get_user(10)))

    def test_name_and_phone_incomplete_without_city(self):
        self.s.ensure_user(10, name="Иван")
        self.s.update_user(10, phone="79134430404")
        self.assertFalse(self.srv._profile_complete(self.s.get_user(10)))

    def test_full_profile_complete(self):
        self.s.ensure_user(10, name="Иван")
        self.s.update_user(10, phone="79134430404", home_city="Черногорск")
        self.assertTrue(self.srv._profile_complete(self.s.get_user(10)))

    def test_short_phone_rejected(self):
        self.s.ensure_user(10, name="Иван")
        self.s.update_user(10, phone="12345", home_city="Черногорск")
        self.assertFalse(self.srv._profile_complete(self.s.get_user(10)))

    def test_phone_normalization(self):
        self.assertEqual(self.srv._normalize_phone("+7 913 443-04-04"), "79134430404")
        self.assertEqual(self.srv._normalize_phone("89134430404"), "79134430404")
        self.assertEqual(self.srv._normalize_phone("9134430404"), "79134430404")

    def test_user_card_exposes_profile_complete(self):
        self.s.ensure_user(10, name="Иван")
        card = self.srv.user_card(10)
        self.assertIn("profile_complete", card)
        self.assertFalse(card["profile_complete"])
        self.s.update_user(10, phone="79134430404", home_city="Черногорск")
        self.assertTrue(self.srv.user_card(10)["profile_complete"])

    def test_order_create_blocked_without_profile(self):
        self.s.ensure_user(10)
        with self.assertRaises(ValueError) as ctx:
            self.srv.act(10, {"op": "order.create", "draft": {
                "pickup": "Черногорск, Ленина 1",
                "destination": "Черногорск, Мира 5",
            }})
        self.assertIn("профиль", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()

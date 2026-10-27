"""Регрессии Mini App: роли, фантомные пользователи, целостность state."""
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class MiniAppRolesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "miniapp.json"),
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

    def test_passenger_role_always_present(self):
        """Пассажир — базовая роль; устаревший флаг passenger_removed её не отбирает."""
        self.s.ensure_user(10)
        self.s.update_user(10, passenger_removed=True,
                           driver_verification_status="approved")
        roles = self.s.compute_roles(10)
        self.assertIn("passenger", roles)
        self.assertIn("driver", roles)

    def test_admin_has_all_three_roles(self):
        """Админ получает все три роли: пассажир + водитель + админ."""
        self.s.ensure_user(10)
        self.s.update_user(10, driver_verification_status="approved")
        # Подменяем список админов, не завися от порядка запуска тестов
        self.s.admin_data["admins"] = [{"admin_id": 10}]
        self.assertTrue(self.s.is_admin(10))
        self.assertEqual(self.s.compute_roles(10), ["passenger", "driver", "admin"])

    def test_plain_passenger_has_single_role(self):
        self.s.ensure_user(10)
        self.assertEqual(self.s.compute_roles(10), ["passenger"])

    def test_active_role_falls_back_when_stale(self):
        self.s.ensure_user(10)
        self.s.update_user(10, active_role="driver")
        self.assertEqual(self.s.get_active_role(10), "passenger")

    def test_ghost_users_hidden_from_admin_lists(self):
        self.s.ensure_user(10)
        self.s.update_user(10, driver_verification_status="approved")
        self.s.users[-1] = {"user_id": -1, "type": "driver",
                            "driver_verification_status": "approved",
                            "name": "Призрак"}
        drivers = self.s.get_all_drivers()
        passengers = self.s.get_all_passengers()
        self.assertFalse(any(int(d["user_id"]) <= 0 for d in drivers))
        self.assertFalse(any(int(p["user_id"]) <= 0 for p in passengers))

    def test_set_active_role_rejects_unknown(self):
        self.s.ensure_user(10)
        self.assertFalse(self.s.set_active_role(10, "driver"))
        self.assertFalse(self.s.set_active_role(10, "superuser"))
        self.assertTrue(self.s.set_active_role(10, "passenger"))


if __name__ == "__main__":
    unittest.main()

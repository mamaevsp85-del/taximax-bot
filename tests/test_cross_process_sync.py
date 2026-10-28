"""Тесты синхронизации базы между процессами (бот + мини-апп)."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import importlib


class CrossProcessSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "sync.json"),
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
        self.path = path
        self.a = self.storage_module.Storage(path)
        self.b = self.storage_module.Storage(path)

    def test_two_instances_share_data(self):
        """Два экземпляра Storage (два процесса) видят данные друг друга."""
        self.a.ensure_user(10)
        self.a.update_user(10, name="Первый", phone="79130000001")
        self.a.mark_onboarded(10)

        # Второй экземпляр должен подхватить при чтении
        u = self.b.get_user(10)
        self.assertEqual(u.get("name"), "Первый")
        self.assertEqual(u.get("phone"), "79130000001")
        self.assertTrue(u.get("onboarded"))

    def test_changes_not_lost_between_instances(self):
        """Изменения одного экземпляра не затираются другим."""
        self.a.ensure_user(10)
        self.a.update_user(10, name="Имя-от-А", home_city="Абакан")

        self.b.ensure_user(20)
        self.b.update_user(20, name="Имя-от-Б", home_city="Черногорск")

        # Оба пользователя должны сохраниться
        self.assertEqual(self.a.get_user(10).get("name"), "Имя-от-А")
        self.assertEqual(self.a.get_user(20).get("name"), "Имя-от-Б")
        self.assertEqual(self.b.get_user(10).get("name"), "Имя-от-А")
        self.assertEqual(self.b.get_user(20).get("name"), "Имя-от-Б")

    def test_orders_shared(self):
        self.a.ensure_user(10)
        oid = self.a.create_order({"passenger_id": 10, "pickup": "A", "destination": "B",
                                   "price": 100})
        self.a.update_order(oid, status="in_progress")

        order = self.b.get_order(oid)
        self.assertEqual(order.get("status"), "in_progress")
        self.assertEqual(order.get("passenger_id"), 10)

    def test_token_visible_across_instances(self):
        self.a.ensure_user(10)
        token = self.a.issue_user_token(10)
        self.assertEqual(self.b.resolve_token(token), 10)

    def test_onboarded_flag_survives(self):
        """Флаг знакомства не теряется между процессами."""
        self.a.ensure_user(10)
        self.a.update_user(10, name="Иван", phone="79134430404")
        self.a.mark_onboarded(10)

        u = self.b.get_user(10)
        self.assertTrue(u.get("onboarded"))


if __name__ == "__main__":
    unittest.main()

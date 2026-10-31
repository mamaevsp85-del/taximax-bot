"""Регрессии: определение пользователя в api_server (_resolve_uid).

Проверяем, что персональный токен имеет приоритет над cookie, чтобы после
перезапуска/переоткрытия Mini-App пользователь не «терялся» и его профиль
не сбрасывался в анонимный.
"""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _request(cookies=None, headers=None, query=None):
    return SimpleNamespace(
        cookies=cookies or {},
        headers=headers or {},
        query=query or {},
        host="127.0.0.1",
        path="/api/state",
    )


class ResolveUidTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "auth.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.api = importlib.import_module("api_server")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.s.ensure_user(10)
        self.s.ensure_user(11)
        self._orig = self.api.storage.storage
        self.api.storage.storage = self.s
        self.api.maximus_server.S = self.s if hasattr(self.api, "maximus_server") else None

    def tearDown(self):
        self.api.storage.storage = self._orig

    def test_cookie_uid(self):
        self.assertEqual(self.api._resolve_uid(_request(cookies={"mx": "10"})), 10)

    def test_query_uid(self):
        self.assertEqual(self.api._resolve_uid(_request(query={"uid": "11"})), 11)

    def test_token_wins_over_cookie(self):
        """Токен надёжнее: старая cookie не должна подменять пользователя."""
        token = self.s.issue_user_token(11)
        req = _request(cookies={"mx": "10", "maximus_token": token})
        self.assertEqual(self.api._resolve_uid(req), 11)

    def test_token_in_query(self):
        token = self.s.issue_user_token(10)
        self.assertEqual(self.api._resolve_uid(_request(query={"token": token})), 10)

    def test_invalid_token_falls_back_to_cookie(self):
        req = _request(cookies={"mx": "10", "maximus_token": "nope"})
        self.assertEqual(self.api._resolve_uid(req), 10)

    def test_anonymous_is_zero(self):
        self.assertEqual(self.api._resolve_uid(_request()), 0)


if __name__ == "__main__":
    unittest.main()
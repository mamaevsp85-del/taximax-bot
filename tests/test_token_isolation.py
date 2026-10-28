"""Тесты изоляции пользователей через персональные токены."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TokenIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "iso.json"),
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

    def test_token_unique_per_user(self):
        self.s.ensure_user(10)
        self.s.ensure_user(20)
        t1 = self.s.issue_user_token(10)
        t2 = self.s.issue_user_token(20)
        self.assertNotEqual(t1, t2)
        self.assertEqual(self.s.resolve_token(t1), 10)
        self.assertEqual(self.s.resolve_token(t2), 20)

    def test_same_token_returned(self):
        self.s.ensure_user(10)
        t1 = self.s.issue_user_token(10)
        t2 = self.s.issue_user_token(10)
        self.assertEqual(t1, t2)

    def test_invalid_token_returns_none(self):
        self.assertIsNone(self.s.resolve_token("invalid"))
        self.assertIsNone(self.s.resolve_token(""))
        self.assertIsNone(self.s.resolve_token(None))

    def test_revoke_token(self):
        self.s.ensure_user(10)
        token = self.s.issue_user_token(10)
        self.assertEqual(self.s.resolve_token(token), 10)
        self.s.revoke_user_token(10)
        self.assertIsNone(self.s.resolve_token(token))

    def test_no_global_last_user(self):
        """Глобального «последний пользователь» больше нет — иначе утечка."""
        self.s.ensure_user(10)
        self.assertNotIn("last_bot_user", self.s.admin_data)
        self.assertFalse(hasattr(self.s, "get_bot_user"))

    def test_token_isolation_between_users(self):
        """Токен A не даёт доступа к данным B."""
        self.s.ensure_user(10)
        self.s.ensure_user(20)
        ta = self.s.issue_user_token(10)
        tb = self.s.issue_user_token(20)
        self.assertEqual(self.s.resolve_token(ta), 10)
        self.assertEqual(self.s.resolve_token(tb), 20)
        self.assertNotEqual(self.s.resolve_token(ta), self.s.resolve_token(tb))

    def test_login_by_token_returns_correct_user(self):
        self.s.ensure_user(10, name="Первый")
        self.s.ensure_user(20, name="Второй")
        ta = self.s.issue_user_token(10)
        res = self.srv.act(0, {"op": "login", "token": ta})
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("user_id"), 10)

    def test_login_with_bad_token_fails(self):
        with self.assertRaises(ValueError):
            self.srv.act(0, {"op": "login", "token": "bogus"})

    def test_uid_from_token_cookie(self):
        self.s.ensure_user(10)
        token = self.s.issue_user_token(10)
        h = MagicMock()
        h.headers = {"Cookie": f"maximus_token={token}"}
        h.path = "/api/state"
        with patch.object(self.storage_module, "storage", self.s):
            self.assertEqual(self.srv.uid_from(h), 10)

    def test_whoami_without_credentials_returns_zero(self):
        h = MagicMock()
        h.headers = {}
        h.path = "/api/whoami"
        self.assertEqual(self.srv.uid_from(h), 0)


if __name__ == "__main__":
    unittest.main()

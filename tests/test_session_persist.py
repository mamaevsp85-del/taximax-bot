"""Тесты сохранения сессии: cookie, uid, отсутствие повторного знакомства."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class SessionPersistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "sess.json"),
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

    def _handler(self, cookie="", path="/api/state"):
        h = MagicMock()
        h.headers = {"Cookie": cookie} if cookie else {}
        h.path = path
        return h

    def test_uid_from_mx_cookie(self):
        h = self._handler(cookie="mx=12345")
        self.assertEqual(self.srv.uid_from(h), 12345)

    def test_uid_from_maximus_uid_cookie(self):
        h = self._handler(cookie="maximus_uid=54321")
        self.assertEqual(self.srv.uid_from(h), 54321)

    def test_uid_from_query(self):
        h = self._handler(path="/api/state?uid=777")
        self.assertEqual(self.srv.uid_from(h), 777)

    def test_uid_from_body(self):
        h = self._handler()
        self.assertEqual(self.srv.uid_from(h, {"uid": 888}), 888)

    def test_uid_priority_cookie_over_query(self):
        h = self._handler(cookie="mx=111", path="/api/state?uid=222")
        self.assertEqual(self.srv.uid_from(h), 111)

    def test_no_uid(self):
        h = self._handler()
        self.assertEqual(self.srv.uid_from(h), 0)

    def test_onboarded_user_not_asked_again(self):
        self.s.ensure_user(10)
        self.s.update_user(10, name="Иван", phone="79134430404",
                           home_city="Черногорск")
        self.s.mark_onboarded(10)
        card = self.srv.user_card(10)
        self.assertTrue(card["onboarded"])
        self.assertFalse(card["needs_onboarding"])

    def test_profile_survives_reload(self):
        """Данные профиля сохраняются в файл и читаются после перезагрузки."""
        self.s.ensure_user(10)
        self.s.update_user(10, name="Иван", phone="79134430404", home_city="Черногорск")
        self.s.mark_onboarded(10)
        self.s.save()

        reloaded = self.storage_module.Storage(self.s.file_path)
        u = reloaded.get_user(10)
        self.assertEqual(u.get("name"), "Иван")
        self.assertEqual(u.get("phone"), "79134430404")
        self.assertEqual(u.get("home_city"), "Черногорск")
        self.assertTrue(u.get("onboarded"))


if __name__ == "__main__":
    unittest.main()

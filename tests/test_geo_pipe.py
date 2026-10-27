"""Тесты «прутка»: закрытие мини-аппа и возврат гео в приложение."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class PipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "pipe.json"),
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
        self.s.ensure_user(10)

    def test_bot_link_from_username(self):
        self.s.admin_data["bot_username"] = "se14566736_bot"
        self.assertEqual(self.srv._bot_link(), "https://max.ru/se14566736_bot")

    def test_bot_link_from_explicit(self):
        self.s.admin_data["bot_link"] = "https://max.ru/custom"
        self.assertEqual(self.srv._bot_link(), "https://max.ru/custom")

    def test_bot_link_empty(self):
        self.s.admin_data.pop("bot_username", None)
        self.s.admin_data.pop("bot_link", None)
        self.assertEqual(self.srv._bot_link(), "")

    def test_user_card_has_bot_link(self):
        self.s.admin_data["bot_username"] = "se14566736_bot"
        card = self.srv.user_card(10)
        self.assertIn("bot_link", card)
        self.assertIn("max.ru", card["bot_link"])

    def test_full_pipe_geo_request_to_done(self):
        """Полный пруток: запрос -> бот получил гео -> приложение забирает."""
        self.s.set_geo_request(10, kind="pickup")
        self.assertEqual(self.s.get_geo_request(10)["status"], "pending")

        # Бот получил точку из чата
        self.s.complete_geo_request(10, 53.81, 91.30,
                                    "Черногорск, Степная улица, 128А", "Черногорск")

        # Приложение опрашивает
        res = self.srv.act(10, {"op": "geo.poll"})
        self.assertEqual(res["status"], "done")
        self.assertAlmostEqual(res["lat"], 53.81)
        self.assertAlmostEqual(res["lon"], 91.30)
        self.assertIn("Степная", res["address"])

        # После получения — очистка
        self.srv.act(10, {"op": "geo.cancel"})
        self.assertEqual(self.srv.act(10, {"op": "geo.poll"})["status"], "none")

    def test_poll_returns_pending_before_geo(self):
        self.s.set_geo_request(10)
        self.assertEqual(self.srv.act(10, {"op": "geo.poll"})["status"], "pending")


if __name__ == "__main__":
    unittest.main()

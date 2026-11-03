"""Избранное: перед сохранением пассажиру предлагают назвать маршрут."""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _cb(payload, actor=20):
    return SimpleNamespace(
        callback=SimpleNamespace(payload=payload, callback_id="cb-test"),
        user=SimpleNamespace(user_id=actor), message=None, actor_id=actor,
        is_bot_started=False, is_bot_added=False, is_message_callback=True,
        is_message_created=False, is_message_edited=False,
        update_type="message_callback")


def _msg(text, actor=20):
    return SimpleNamespace(
        callback=None, user=SimpleNamespace(user_id=actor),
        message=SimpleNamespace(text=text, location=None, image=None, contact=None),
        actor_id=actor, is_bot_started=False, is_bot_added=False,
        is_message_callback=False, is_message_created=True,
        is_message_edited=False, update_type="message_created")


def _buttons(texts_kwargs):
    out = []
    for kw in texts_kwargs:
        for row in (kw.get("keyboard") or []):
            for btn in row:
                out.append(btn.get("text") or "")
    return out


class FavoriteNamingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "favname.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.passenger_mod = importlib.import_module("handlers.passenger_new")
        cls.reply_module = importlib.import_module("ui.reply")
        cls.router_class = importlib.import_module("core.router").Router

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.reply_module._MENU_MID.clear()
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        for mod, name in ((self.storage_module, "storage"),
                          (self.passenger_mod, "S")):
            p = patch.object(mod, name, self.s)
            p.start()
            self.addCleanup(p.stop)
        self.client = AsyncMock()
        self.client.send_message.return_value = {"message_id": "m1"}
        self.client.edit_message = AsyncMock(return_value={"message_id": "m1"})
        self.client.answer_callback = AsyncMock(return_value=True)
        self.client._request = AsyncMock(return_value={"messages": [], "marker": None})
        self.router = self.router_class(self.client)
        self.passenger_mod.register_passenger(self.router)
        self.s.ensure_user(20, name="Пассажир")
        self.s.update_user(20, phone="+79130000000", home_city="Черногорск",
                           onboarded=True)
        self.oid = self.s.create_order({
            "passenger_id": 20, "pickup": "Черногорск, Мира 1",
            "destination": "Черногорск, Степная 12",
            "price": 200, "base_price": 200, "distance": 5.0,
            "time_minutes": 10, "status": "completed",
        })

    def _texts(self):
        out = []
        for call in self.client.send_message.call_args_list:
            text = call.kwargs.get("text", "")
            if text:
                out.append(text)
        for call in self.client.edit_message.call_args_list:
            for arg in call.args:
                if isinstance(arg, str):
                    out.append(arg)
        return out

    def _buttons(self):
        found = []
        calls = (list(self.client.send_message.call_args_list)
                 + list(self.client.edit_message.call_args_list))
        for call in calls:
            for att in (call.kwargs.get("attachments") or []):
                if not isinstance(att, dict) or att.get("type") != "inline_keyboard":
                    continue
                for row in att.get("payload", {}).get("buttons", []):
                    for btn in row:
                        if isinstance(btn, dict):
                            found.append(btn.get("text") or "")
        return found

    async def test_favorite_add_asks_for_name(self):
        await self.router.callback_handlers["passenger:fav_add:"](
            self.client, 20, _cb(f"passenger:fav_add:{self.oid}", 20), {})
        blob = " ".join(self._texts())
        self.assertIn("Как назвать", blob)
        buttons = self._buttons()
        self.assertIn("💼 На работу", buttons)
        self.assertIn("🏠 Домой", buttons)
        # Ничего не сохранили до выбора названия
        self.assertEqual(self.s.get_favorites(20), [])

    async def test_preset_button_saves_that_label(self):
        await self.router.callback_handlers["passenger:fav_add:"](
            self.client, 20, _cb(f"passenger:fav_add:{self.oid}", 20), {})
        presets = self.passenger_mod.FAVORITE_PRESETS
        await self.router.callback_handlers["passenger:fav_name_set:"](
            self.client, 20, _cb("passenger:fav_name_set:1", 20), {})
        favs = self.s.get_favorites(20)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["label"], presets[1])

    async def test_skip_saves_route_label(self):
        await self.router.callback_handlers["passenger:fav_add:"](
            self.client, 20, _cb(f"passenger:fav_add:{self.oid}", 20), {})
        await self.router.callback_handlers["passenger:fav_name_skip"](
            self.client, 20, _cb("passenger:fav_name_skip", 20), {})
        favs = self.s.get_favorites(20)
        self.assertEqual(len(favs), 1)
        self.assertIn("Мира 1", favs[0]["label"])

    async def test_adding_without_name_flow_keeps_default(self):
        """Если имя не выбрали — маршрут всё равно сохраняется (без падений)."""
        await self.router.callback_handlers["passenger:fav_add:"](
            self.client, 20, _cb(f"passenger:fav_add:{self.oid}", 20), {})
        await self.router.callback_handlers["passenger:fav_name_skip"](
            self.client, 20, _cb("passenger:fav_name_skip", 20), {})
        self.assertEqual(len(self.s.get_favorites(20)), 1)

    async def test_custom_text_name_saved(self):
        """Можно ввести своё название текстом вместо готового варианта."""
        await self.router.callback_handlers["passenger:fav_add:"](
            self.client, 20, _cb(f"passenger:fav_add:{self.oid}", 20), {})
        await self.router._handle_message(20, _msg("На дачу", 20))
        favs = self.s.get_favorites(20)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["label"], "На дачу")


if __name__ == "__main__":
    unittest.main()
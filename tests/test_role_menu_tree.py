"""Дерево меню и сценарии трёх ролей в одном боте.

Проверяет, что выбор роли сохранён, а каждая кнопка пассажира, водителя
и админа ведёт в зарегистрированный обработчик и не падает.
"""
import ast
import os
import re
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


def _callback_payloads(path: Path) -> set[str]:
    """Литералы и префиксы payload из callback_button(...)."""
    text = path.read_text(encoding="utf-8")
    found: set[str] = set()
    for match in re.finditer(
        r"callback_button\(\s*(?:f)?([\"']).*?\1\s*,\s*(?:f)?([\"'])(.*?)\2",
        text, re.S,
    ):
        raw = match.group(3)
        if "{" in raw:
            found.add(raw.split("{", 1)[0])
        else:
            found.add(raw)
    return {p for p in found if p}


class RoleMenuTreeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "bootstrap.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        import importlib
        cls.storage_module = importlib.import_module("core.storage")
        cls.support = importlib.import_module("core.driver_support")
        cls.passenger = importlib.import_module("handlers.passenger_new")
        cls.driver = importlib.import_module("handlers.driver")
        cls.admin = importlib.import_module("handlers.admin")
        cls.common = importlib.import_module("handlers.common")
        cls.router_class = importlib.import_module("core.router").Router
        cls.reply_module = importlib.import_module("ui.reply")
        cls.fsm = importlib.import_module("core.fsm")
        cls.geocoder = importlib.import_module("core.geocoder")
        cls.settings = importlib.import_module("config").settings
        cls.Update = importlib.import_module("max_api.models").Update
        cls.User = importlib.import_module("max_api.models").User
        cls.Message = importlib.import_module("max_api.models").Message
        cls.Callback = importlib.import_module("max_api.models").Callback

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.reply_module._MENU_MID.clear()
        self.settings.admin_user_ids = "99"
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self._patches = []
        for module, name in (
            (self.storage_module, "storage"),
            (self.support, "S"),
            (self.passenger, "S"),
            (self.driver, "S"),
            (self.admin, "S"),
        ):
            p = patch.object(module, name, self.s)
            p.start()
            self._patches.append(p)
        self.addCleanup(self._stop_patches)
        self.client = AsyncMock()
        self.client.send_message.return_value = {"message_id": "m1"}
        self.client.edit_message.return_value = {"message_id": "m1"}
        self.client.answer_callback.return_value = {}
        self.client.delete_message.return_value = {}
        self.client._request.return_value = {"messages": []}
        self.router = self.router_class(self.client)
        self.common.register_common(self.router)
        self.passenger.register_passenger(self.router)
        self.driver.register_driver(self.router)
        self.admin.register_admin(self.router)

        async def geocode_address(address):
            return {"lat": 53.72, "lon": 91.43}, "Абакан"

        async def get_address_by_coordinates(lat, lon):
            return "г. Абакан, ул. Ленина 1", "Абакан"

        async def calculate_distance_and_time(*args, **kwargs):
            return 3.4, 9

        for name, fn in (
            ("geocode_address", geocode_address),
            ("get_address_by_coordinates", get_address_by_coordinates),
            ("calculate_distance_and_time", calculate_distance_and_time),
        ):
            p = patch.object(self.geocoder, name, fn)
            p.start()
            self._patches.append(p)
            if hasattr(self.passenger.geocoder, name):
                p2 = patch.object(self.passenger.geocoder, name, fn)
                p2.start()
                self._patches.append(p2)
            if hasattr(self.driver, name):
                p3 = patch.object(self.driver, name, fn)
                p3.start()
                self._patches.append(p3)

    def _stop_patches(self):
        for p in reversed(self._patches):
            p.stop()

    def _upd(self, user_id, payload):
        return SimpleNamespace(
            callback=SimpleNamespace(payload=payload, callback_id=f"cb-{payload}"),
            user=SimpleNamespace(user_id=user_id, name="Тест"),
            message=None,
        )

    def _resolve(self, payload):
        fn = self.router.callback_handlers.get(payload)
        if fn is not None:
            return fn
        for key, handler in self.router.callback_handlers.items():
            if key.endswith(":") and payload.startswith(key):
                return handler
        return None

    def _buttons(self):
        found = []
        calls = (
            list(self.client.send_message.call_args_list)
            + list(self.client.edit_message.call_args_list)
        )
        for call in calls:
            attachments = call.kwargs.get("attachments") or []
            for att in attachments:
                if not isinstance(att, dict) or att.get("type") != "inline_keyboard":
                    continue
                for row in att.get("payload", {}).get("buttons", []):
                    for btn in row:
                        if isinstance(btn, dict) and btn.get("type") == "callback":
                            found.append((btn.get("text") or "", btn.get("payload") or ""))
        return found

    async def _click(self, user_id, payload):
        fn = self._resolve(payload)
        if fn is None:
            raise AssertionError(f"нет обработчика для {payload!r}")
        await fn(self.client, user_id, self._upd(user_id, payload), {})

    async def _walk(self, user_id, seeds):
        seen = set()
        queue = list(seeds)
        errors = []
        while queue and len(seen) < 250:
            payload = queue.pop(0)
            if not payload or payload in seen or payload == "a:noop":
                continue
            seen.add(payload)
            before = len(self.client.send_message.call_args_list) + len(
                self.client.edit_message.call_args_list
            )
            try:
                await self._click(user_id, payload)
            except Exception as exc:
                errors.append(f"{payload}: {type(exc).__name__}: {exc}")
                continue
            calls_now = (
                list(self.client.send_message.call_args_list)
                + list(self.client.edit_message.call_args_list)
            )
            for text, nxt in self._buttons_from(calls_now[before:]):
                if nxt and nxt not in seen:
                    queue.append(nxt)
        return seen, errors

    def _buttons_from(self, calls):
        found = []
        for call in calls:
            attachments = call.kwargs.get("attachments") or []
            for att in attachments:
                if not isinstance(att, dict) or att.get("type") != "inline_keyboard":
                    continue
                for row in att.get("payload", {}).get("buttons", []):
                    for btn in row:
                        if isinstance(btn, dict) and btn.get("type") == "callback":
                            found.append((btn.get("text") or "", btn.get("payload") or ""))
        return found

    def _texts(self):
        out = []
        for call in self.client.send_message.call_args_list:
            out.append(call.kwargs.get("text") or "")
        for call in self.client.edit_message.call_args_list:
            out.append(call.kwargs.get("text") or "")
        return "\n".join(out)

    async def test_every_literal_button_is_registered(self):
        files = [
            ROOT / "handlers" / "passenger_new.py",
            ROOT / "handlers" / "driver.py",
            ROOT / "handlers" / "admin.py",
            ROOT / "core" / "driver_support.py",
            ROOT / "core" / "router.py",
            ROOT / "handlers" / "common.py",
        ]
        missing = []
        for path in files:
            for payload in _callback_payloads(path):
                if self._resolve(payload) is None and self._resolve(payload + "x") is None:
                    # префикс 'a:drv:view:' резолвится как 'a:'
                    if payload.endswith(":") and self._resolve(payload + "1") is None:
                        missing.append(f"{path.name}: {payload}")
                    elif not payload.endswith(":"):
                        missing.append(f"{path.name}: {payload}")
        self.assertEqual(missing, [], "кнопки без обработчика:\n" + "\n".join(missing))

    async def test_role_selector_stays_single_bot(self):
        self.s.ensure_user(1, name="Пассажир", username="p")
        self.s.update_user(1, phone="79990000001")
        start = self.Update(
            update_type="bot_started", timestamp=1,
            user=self.User(user_id=1, name="Пассажир"),
        )
        await self.router.dispatch(start)
        texts = self._texts()
        self.assertNotIn("Выберите роль", texts)
        self.assertIn("Создать поездку", self._joined_buttons())

        self.s.ensure_user(2, name="Водитель", username="d")
        self.s.update_user(2, phone="79990000002", driver_verification_status="approved",
                           car_number="А111АА19", car_model="Kia")
        self.client.send_message.reset_mock()
        self.client.edit_message.reset_mock()
        start.user = self.User(user_id=2, name="Водитель")
        await self.router.dispatch(start)
        labels = self._joined_buttons()
        self.assertIn("Выберите роль", self._texts())
        self.assertIn("Пассажир", labels)
        self.assertIn("Водитель", labels)
        self.assertNotIn("Админ", labels)

        self.s.ensure_user(99, name="Админ", username="a")
        self.s.update_user(99, phone="79990000099")
        self.client.send_message.reset_mock()
        self.client.edit_message.reset_mock()
        start.user = self.User(user_id=99, name="Админ")
        await self.router.dispatch(start)
        labels = self._joined_buttons()
        self.assertIn("Пассажир", labels)
        self.assertIn("Админ", labels)
        self.assertNotIn("Водитель", labels)

        await self._click(99, "role:admin")
        self.assertIn("Панель администратора", self._texts())
        await self._click(99, "menu:roles")
        self.assertIn("Выберите роль", self._texts())
        await self._click(99, "role:passenger")
        self.assertIn("Создать поездку", self._joined_buttons())
        await self._click(2, "role:driver")
        self.assertIn("Текущие заказы", self._joined_buttons())

    def _joined_buttons(self):
        return " | ".join(text for text, _ in self._buttons())

    async def test_passenger_menu_tree_and_order(self):
        uid = 20
        self.s.ensure_user(uid, name="Иван", username="ivan")
        self.s.update_user(uid, phone="79991112233")
        self.s.ensure_user(10, name="Водитель", username="drv")
        self.s.update_user(10, phone="79990000010", driver_verification_status="approved",
                           status="online", car_model="Toyota", car_number="А123ВС19")
        seen, errors = await self._walk(uid, ["passenger:menu"])
        self.assertEqual(errors, [])
        for label in ("Создать поездку", "Мои заказы", "История заказов",
                      "Профиль", "Статистика", "Стать водителем"):
            self.assertIn(label, self._joined_buttons() + self._texts())
        self.assertIn("passenger:new_order", seen)
        self.assertIn("passenger:become_driver", seen)
        self.assertIn("passenger:stats", seen)

        self.client.send_message.reset_mock()
        await self._click(uid, "passenger:new_order")
        pickup = SimpleNamespace(message=SimpleNamespace(
            text="г. Абакан, ул. Ленина 1", location=None))
        await self.router.state_handlers["passenger:await_pickup"](
            self.client, uid, pickup, {})
        dest = SimpleNamespace(message=SimpleNamespace(
            text="г. Абакан, пр. Мира 5", location=None))
        await self.router.state_handlers["passenger:await_dest"](
            self.client, uid, dest, {})
        self.assertIn("Подтверждение заказа", self._texts())
        await self._click(uid, "passenger:confirm_order")
        orders = [o for o in self.s.orders.values() if o.get("passenger_id") == uid]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "pending")
        self.assertGreater(orders[0]["price"], 0)

        oid = orders[0]["id"]
        await self._click(10, f"order:accept:{oid}")
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "accepted")
        self.assertEqual(order["driver_id"], 10)
        await self._click(10, f"driver:arrived:{oid}")
        self.assertEqual(self.s.get_order(oid)["status"], "driver_arrived")
        await self._click(10, f"driver:start_driving:{oid}")
        self.assertEqual(self.s.get_order(oid)["status"], "in_progress")
        await self._click(10, f"driver:complete_trip:{oid}")
        self.assertEqual(self.s.get_order(oid)["status"], "in_progress")
        geo = SimpleNamespace(message=SimpleNamespace(
            location=SimpleNamespace(latitude=53.73, longitude=91.45), text=None))
        await self.router.state_handlers["driver:await_final_geo"](
            self.client, 10, geo, {})
        self.assertEqual(self.s.get_order(oid)["status"], "completed")
        await self._click(uid, f"passenger:rate:{oid}:5")

    async def test_driver_menu_tree_and_line(self):
        uid = 10
        self.s.ensure_user(uid, name="Водитель", username="drv")
        self.s.update_user(
            uid, phone="79990000010", driver_verification_status="approved",
            car_model="Kia", car_number="О777ОО19", car_color="белый",
            driver_name="Водитель", driver_phone="79990000010",
        )
        seen, errors = await self._walk(uid, ["driver:menu"])
        self.assertEqual(errors, [])
        blob = self._joined_buttons()
        for label in ("Текущие заказы", "Список заказов", "Обновить гео",
                      "Мой профиль", "Сменить роль", "Выйти на линию"):
            self.assertIn(label, blob)
        self.assertIn("driver:profile", seen)
        self.assertIn("driver:orders_available", seen)
        self.assertIn("driver:stats", seen)
        self.assertIn("driver:docs", seen)
        await self._click(uid, "driver:online")
        self.assertEqual(self.s.get_user(uid).get("status"), "online")
        await self._click(uid, "driver:break")
        self.assertEqual(self.s.get_user(uid).get("status"), "break")
        await self._click(uid, "driver:offline")
        self.assertEqual(self.s.get_user(uid).get("status"), "offline")

    async def test_admin_menu_tree_and_moderation(self):
        admin = 99
        self.s.ensure_user(admin, name="Админ", username="admin")
        self.s.ensure_user(7, name="Кандидат", username="cand")
        self.s.update_user(7, phone="79990000007", driver_verification_status="pending",
                           car_number="Т001ТТ19", car_model="Lada", car_color="синий")
        seen, errors = await self._walk(admin, ["a:menu"])
        self.assertEqual(errors, [])
        blob = self._joined_buttons() + self._texts()
        for label in (
            "Управление водителями", "Управление заказами", "Управление пассажирами",
            "Управление тарифами", "Статистика ботов", "Менеджеры",
            "Настройки системы", "Настройка комиссии", "Модерация водителей",
            "Контент и рассылка", "Сменить роль",
        ):
            self.assertIn(label, blob)
        self.assertIn("a:drivers", seen)
        self.assertIn("a:orders", seen)
        self.assertIn("a:tariffs", seen)
        self.assertIn("a:commission", seen)
        self.assertIn("a:content", seen)
        self.assertIn("a:moderation", seen)
        await self._click(admin, "a:drv:approve:7")
        self.assertEqual(self.s.get_user(7).get("driver_verification_status"), "approved")
        self.assertIn("driver", self.s.compute_roles(7))

    async def test_handlers_parse(self):
        for rel in ("handlers/passenger_new.py", "handlers/driver.py",
                    "handlers/admin.py", "handlers/common.py", "core/router.py"):
            ast.parse((ROOT / rel).read_text(encoding="utf-8"))

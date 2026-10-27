"""Offline regression tests. Never load or write the production storage file."""
import asyncio
from datetime import datetime, timedelta
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "bootstrap.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.support = importlib.import_module("core.driver_support")
        cls.driver = importlib.import_module("handlers.driver")
        cls.admin = importlib.import_module("handlers.admin")
        cls.passenger = importlib.import_module("handlers.passenger_new")
        cls.router_class = importlib.import_module("core.router").Router
        cls.reply_module = importlib.import_module("ui.reply")
        cls.common = importlib.import_module("handlers.common")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.reply_module._MENU_MID.clear()
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        for module, name in ((self.storage_module, "storage"),
                             (self.support, "S"), (self.driver, "S"),
                             (self.admin, "S"), (self.passenger, "S")):
            p = patch.object(module, name, self.s)
            p.start()
            self.addCleanup(p.stop)
        self.client = AsyncMock()
        self.client.send_message.return_value = {"message_id": "test-message"}
        self.router = self.router_class(self.client)

    def order(self, status="accepted", driver_id=10):
        self.s.ensure_user(10)
        self.s.update_user(10, driver_verification_status="approved", status="online")
        oid = self.s.create_order({"passenger_id": 20, "driver_id": driver_id,
                                   "price": 100})
        self.s.update_order(oid, status=status)
        self.s.update_user(10, driver_current_order_id=oid)
        return oid

    def update(self, payload, actor=10):
        from types import SimpleNamespace
        return SimpleNamespace(callback=SimpleNamespace(payload=payload,
                                                        callback_id="test"),
                               user=SimpleNamespace(user_id=actor))

    async def test_admin_registration_and_menu(self):
        self.admin.register_admin(self.router)
        self.assertIn("admin", self.router.role_menu)
        self.assertIn("a:", self.router.callback_handlers)
        await self.router.role_menu["admin"](self.client, 99, None, {})
        self.client.send_message.assert_awaited()

    async def test_admin_callback_dispatch(self):
        self.admin.register_admin(self.router)
        await self.router.callback_handlers["a:"](
            self.client, 99, self.update("a:menu", 99), {})
        self.client.send_message.assert_awaited()

    async def test_driver_main_menu_layout(self):
        self.s.ensure_user(10)
        self.s.update_user(10, driver_verification_status="approved", status="online")
        payloads = [b["payload"] for row in self.support.main_menu_rows(10) for b in row]
        self.assertEqual(payloads, [
            "driver:work_status",
            "driver:current_order",
            "driver:orders_available",
            "driver:location",
            "driver:profile",
            "menu:roles",
        ])
        self.assertNotIn("driver:auto_loc", payloads)

    async def test_balance_hidden_when_commission_disabled(self):
        self.s.ensure_user(10)
        self.s.disable_commission_for_all()
        self.assertTrue(self.support.balance_hidden())
        profile_payloads = [b["payload"] for row in self.support.profile_rows() for b in row]
        self.assertNotIn("driver:balance", profile_payloads)
        self.assertNotIn("Авто-гео", str(profile_payloads))

    async def test_balance_visible_when_commission_enabled(self):
        self.s.ensure_user(10)
        self.s.enable_commission()
        self.assertFalse(self.support.balance_hidden())
        profile_payloads = [b["payload"] for row in self.support.profile_rows() for b in row]
        self.assertIn("driver:balance", profile_payloads)
        self.s.ensure_user(10)
        for location in (None, "legacy address", {}, {"city": "Москва"}):
            self.s.update_user(10, last_location=location)
            self.assertIsInstance(self.support.driver_main_header(10), str)

    async def test_waiting_preserved_and_stop_idempotent(self):
        oid = self.order()
        self.s.update_order(oid, waiting_started_at=(datetime.now() - timedelta(minutes=10)).isoformat(),
                            waiting_seconds_total=60)
        before = self.support.calculate_waiting_cost(oid)
        self.support.stop_waiting(oid)
        after = self.support.calculate_waiting_cost(oid)
        self.assertGreaterEqual(after, 40)
        self.assertAlmostEqual(before, after, delta=0.1)
        self.support.stop_waiting(oid)
        self.assertEqual(after, self.support.calculate_waiting_cost(oid))

    async def test_repeated_wait_start_does_not_reset_timer(self):
        oid = self.order()
        self.support.start_waiting(oid)
        started = self.s.get_order(oid)["waiting_started_at"]
        try:
            self.support.start_waiting(oid)
            self.assertEqual(started, self.s.get_order(oid)["waiting_started_at"])
        finally:
            self.support.stop_waiting(oid)

    async def test_stale_or_foreign_order_not_current(self):
        oid = self.order(status="cancelled")
        self.assertIsNone(self.support.get_driver_current_order(10))
        self.s.update_order(oid, status="accepted", driver_id=11)
        self.assertIsNone(self.support.get_driver_current_order(10))

    async def test_location_update_saves_structured_geo(self):
        self.s.ensure_user(10)
        from types import SimpleNamespace
        update = SimpleNamespace(
            message=SimpleNamespace(location=SimpleNamespace(latitude=53.81, longitude=91.30)),
            callback=None,
        )
        with patch.object(self.driver, "get_address_by_coordinates",
                          new=AsyncMock(return_value=("Черногорск, Степная 1", "Черногорск"))):
            await self.driver.driver_location_input(self.client, 10, update, {})
        loc = self.s.get_user(10)["last_location"]
        self.assertEqual(loc["latitude"], 53.81)
        self.assertEqual(loc["longitude"], 91.30)
        self.assertEqual(loc["city"], "Черногорск")
        self.assertEqual(loc["address"], "Черногорск, Степная 1")
        self.assertTrue(loc["updated_at"])
        self.assertEqual(self.s.get_user(10)["current_city"], "Черногорск")

    async def test_complete_trip_requests_geo_then_recalculates_on_geo(self):
        oid = self.order(status="in_progress")
        oid = self.s.get_order(oid)["id"]
        from types import SimpleNamespace
        self.s.update_order(oid, destination_coords={"latitude": 55.0, "longitude": 37.0},
                            pickup_coords={"latitude": 53.8, "longitude": 91.2},
                            distance=20.0, time_minutes=30, price=300)
        update = self.update(f"driver:complete_trip:{oid}", 10)
        # Нажатие «Завершить поездку»: бот просит гео, а не завершает сразу
        await self.driver.driver_complete_trip(self.client, 10, update, {})
        fsm_loaded = self.driver.get_context(10)
        self.assertEqual(fsm_loaded._store.get_state(10), "driver:await_final_geo")
        self.assertEqual(fsm_loaded._store.get_data(10).get("final_geo_order_id"), oid)
        # статус заказа ещё НЕ изменён
        self.assertNotEqual(self.s.get_order(oid)["status"], "completed")

        # Водитель присылает гео на точке высадки
        geo_update = SimpleNamespace(message=SimpleNamespace(
            location=SimpleNamespace(latitude=55.0, longitude=37.0)))
        with patch.object(self.driver, "get_address_by_coordinates",
                          new=AsyncMock(return_value=("Москва, Тверская 1", "Москва"))), \
             patch.object(self.driver, "calculate_distance_and_time",
                          new=AsyncMock(return_value=(5.0, 8))):
            await self.driver.driver_trip_final_geo(self.client, 10, geo_update, {})

        order = self.s.get_order(oid)
        # Заказ завершён, стоимость пересчитана по новому расстоянию (5 км)
        self.assertEqual(order["status"], "completed")
        self.assertAlmostEqual(float(order["distance"]), 5.0)
        self.assertEqual(order["destination_coords"],
                         {"lat": 55.0, "lon": 37.0})
        self.assertEqual(order["destination"], "Москва, Тверская 1")
        # пересчёт: 5км > included_km(2) -> 100 + 3*20 + 8*2 = 176, округление ↑ до 180
        self.assertEqual(int(order["price"]), 180)
        self.assertTrue(order.get("recalculated_at"))
        # водитель отмечен на конечном адресе
        loc = self.s.get_user(10)["last_location"]
        self.assertEqual(loc["lat"], 55.0)
        self.assertEqual(loc["city"], "Москва")
        # пассажиру отправлен детальный отчёт
        self.assertTrue(self.client.send_message.called)
        msg_text = self.client.send_message.call_args.kwargs["text"]
        self.assertIn("Москва, Тверская 1", msg_text)
        self.assertIn("5.0 км", msg_text)
        # После завершения водитель снова свободен
        self.assertEqual(fsm_loaded._store.get_state(10), "STATE_IDLE")

    async def test_complete_trip_recalc_uses_lat_lon_pickup_coords(self):
        # Реальный формат pickup_coords от geocoder / passenger_new: {"lat","lon"}
        from types import SimpleNamespace
        oid = self.order(status="in_progress")
        oid = self.s.get_order(oid)["id"]
        self.s.update_order(oid, pickup_coords={"lat": 53.8, "lon": 91.2},
                            destination_coords={"latitude": 55.0, "longitude": 37.0},
                            distance=20.0, time_minutes=30, price=300)
        geo_update = SimpleNamespace(message=SimpleNamespace(
            location=SimpleNamespace(latitude=55.0, longitude=37.0)))
        with patch.object(self.driver, "get_address_by_coordinates",
                          new=AsyncMock(return_value=("Москва, Тверская 1", "Москва"))), \
             patch.object(self.driver, "calculate_distance_and_time",
                          new=AsyncMock(return_value=(5.0, 8))):
            await self.driver.driver_trip_final_geo(self.client, 10, geo_update, {})
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "completed")
        self.assertAlmostEqual(float(order["distance"]), 5.0)
        self.assertEqual(int(order["price"]), 180)
        self.assertTrue(order.get("recalculated_at"))

    async def test_complete_trip_zero_distance_recalc_applied(self):
        # pickup == dropoff: маршрутизатор может вернуть 0.0 км — пересчёт
        # всё равно должен примениться (раньше `if calc and calc[0]` пропускал 0.0).
        from types import SimpleNamespace
        oid = self.order(status="in_progress")
        oid = self.s.get_order(oid)["id"]
        self.s.update_order(oid, pickup_coords={"lat": 53.8, "lon": 91.2},
                            destination_coords={"latitude": 53.8, "longitude": 91.2},
                            distance=20.0, time_minutes=30, price=300)
        geo_update = SimpleNamespace(message=SimpleNamespace(
            location=SimpleNamespace(latitude=53.8, longitude=91.2)))
        with patch.object(self.driver, "get_address_by_coordinates",
                          new=AsyncMock(return_value=("Черногорск, Степная 128А", "Черногорск"))), \
             patch.object(self.driver, "calculate_distance_and_time",
                          new=AsyncMock(return_value=(0.0, 0))):
            await self.driver.driver_trip_final_geo(self.client, 10, geo_update, {})
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "completed")
        self.assertAlmostEqual(float(order["distance"]), 0.0)
        self.assertEqual(int(order["time_minutes"]), 0)
        self.assertTrue(order.get("recalculated_at"))

    async def test_notify_passenger_on_driver_assign(self):
        oid = self.order(status="pending")
        self.s.update_order(oid, price=300)
        update = self.update("order:accept:%d" % oid, 10)  # водитель 10 (approved)
        # passenger_id задан в self.order
        await self.driver.driver_accept_order(self.client, 10, update, {})
        # пассажиру отправлено уведомление о назначении водителя
        self.assertTrue(self.client.send_message.called)
        texts = [c.kwargs.get("text", "") for c in self.client.send_message.call_args_list]
        self.assertTrue(any("принял водитель" in t for t in texts))
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "accepted")

    async def test_complete_trip_final_geo_requires_location(self):
        oid = self.order(status="in_progress")
        from types import SimpleNamespace
        await self.driver.driver_complete_trip(self.client, 10,
                                               self.update(f"driver:complete_trip:{oid}", 10), {})
        # без локации — ошибка, заказ НЕ завершается
        bad = SimpleNamespace(message=SimpleNamespace(text="не координаты", location=None))
        await self.driver.driver_trip_final_geo(self.client, 10, bad, {})
        self.assertNotEqual(self.s.get_order(oid)["status"], "completed")

    async def test_foreign_driver_cannot_mutate_order(self):
        oid = self.order()
        for fn, payload in (
            (self.driver.driver_cancel_order, "driver:cancel_order:"),
            (self.driver.driver_wait_start, "driver:wait_start:"),
            (self.driver.driver_wait_stop, "driver:wait_stop:"),
        ):
            before = dict(self.s.get_order(oid))
            await fn(self.client, 11, self.update(f"{payload}{oid}", 11), {})
            self.assertEqual(before, self.s.get_order(oid))

    async def test_offline_driver_cannot_accept(self):
        oid = self.order(status="pending", driver_id=None)
        self.s.update_user(10, status="offline", driver_current_order_id=None)
        await self.driver._accept_order(self.client, 10, oid)
        self.assertEqual(self.s.get_order(oid)["status"], "pending")

    async def test_wait_cancel_cannot_cancel_in_progress(self):
        oid = self.order(status="in_progress")
        self.passenger.register_passenger(self.router)
        await self.router.callback_handlers["passenger:cancel_wait:"](
            self.client, 20, self.update(f"passenger:cancel_wait:{oid}", 20), {})
        self.assertEqual(self.s.get_order(oid)["status"], "in_progress")

    async def test_atomic_save_retries_windows_file_lock(self):
        original = os.replace
        with patch.object(self.storage_module.os, "replace",
                          side_effect=[PermissionError("locked"), None]) as replace:
            self.s.save()
            self.assertEqual(replace.call_count, 2)
        self.s.save()
        self.assertEqual(json.loads(Path(self.s.file_path).read_text(encoding="utf-8"))["orders"], {})
        self.assertFalse(list(Path(self.temp.name).glob("*.tmp")))

    async def test_driver_go_online_does_not_crash(self):
        """Выход на линию не должен падать (регрессия: geo_location_rows)."""
        self.s.ensure_user(10)
        self.s.update_user(10, driver_verification_status="approved", status="offline")
        # Комиссия по умолчанию выключена -> доступ к линии есть.
        await self.driver.driver_set_status(
            self.client, 10, self.update("driver:online", 10), {})
        self.assertEqual(self.s.get_user(10).get("status"), "online")
        self.client.send_message.assert_awaited()

    async def test_order_card_uses_pickup_fallback_and_growing_wait_cost(self):
        # Старый формат хранения адресов (pickup/destination) должен отображаться.
        oid = self.s.create_order({"passenger_id": 20, "pickup": "Абакан, Ленина 1",
                                   "destination": "Абакан, Мира 5", "price": 100,
                                   "distance": 3.0})
        order = self.s.get_order(oid)
        text = self.support.build_order_card_text(order)
        self.assertIn("Абакан, Ленина 1", text)
        self.assertIn("Абакан, Мира 5", text)
        self.assertNotIn("Откуда: —", text)
        self.assertNotIn("Куда: —", text)

        # Платное ожидание: старт 10 минут назад -> сумма > 0 и корректная.
        started = (datetime.now() - timedelta(minutes=10)).isoformat()
        self.s.update_order(oid, waiting_started_at=started, waiting_ended_at=None,
                            status="driver_arrived")
        info = self.support.get_waiting_info(oid)
        self.assertFalse(info["is_free"])
        self.assertGreater(info["cost"], 30)
        self.assertLess(info["cost"], 40)  # ~(10-3)*5 = 35
        self.assertIn("₽", self.support.format_waiting_line(info))

        # stop_waiting фиксирует накопленную сумму в заказе.
        self.support.stop_waiting(oid)
        saved = self.s.get_order(oid)
        self.assertIsNone(saved.get("waiting_started_at"))
        self.assertGreater(saved.get("waiting_cost", 0), 30)


if __name__ == "__main__":
    unittest.main()
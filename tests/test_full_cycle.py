"""Полное сквозное тестирование цикла пассажир <-> водитель."""
from __future__ import annotations
import asyncio, importlib, os, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from core.storage import haversine_distance, get_available_orders_for_driver

def _make_update(payload=None, actor=20, text=None, location=None, image=None):
    cb = SimpleNamespace(payload=payload, callback_id="cb-test") if payload is not None else None
    msg = SimpleNamespace(text=text, location=location, image=image, contact=None) if (text is not None or location is not None or image is not None) else None
    return SimpleNamespace(callback=cb, user=SimpleNamespace(user_id=actor), message=msg, actor_id=actor,
                           is_bot_started=False, is_bot_added=False, is_message_callback=cb is not None,
                           is_message_created=msg is not None, is_message_edited=False,
                           update_type="message_callback" if cb else "message_created")

def _calls_texts(client):
    return [c.kwargs.get("text", "") for c in client.send_message.call_args_list]

class FullCycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "full_cycle.json"),
            "MAX_BOT_TOKEN": "offline-test-token", "ADMIN_USER_IDS": "99",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.passenger_mod = importlib.import_module("handlers.passenger_new")
        cls.driver_mod = importlib.import_module("handlers.driver")
        cls.common_mod = importlib.import_module("handlers.common")
        cls.driver_support_mod = importlib.import_module("core.driver_support")
        cls.router_class = importlib.import_module("core.router").Router
        cls.reply_module = importlib.import_module("ui.reply")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop(); cls.temp.cleanup()

    def setUp(self):
        self.reply_module._MENU_MID.clear()
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        for mod, name in ((self.storage_module, "storage"), (self.passenger_mod, "S"),
                          (self.driver_mod, "S"), (self.driver_support_mod, "S")):
            p = patch.object(mod, name, self.s); p.start(); self.addCleanup(p.stop)
        self.client = AsyncMock()
        self.client.send_message.return_value = {"message_id": "test-msg"}
        self.client.answer_callback = AsyncMock(return_value=True)
        self.client._request = AsyncMock(return_value={"messages": [], "marker": None})
        self.router = self.router_class(self.client)
        self.passenger_mod.register_passenger(self.router)
        self.driver_mod.register_driver(self.router)
        self.common_mod.register_common(self.router)
        self.s.ensure_user(20, name="Пассажир", username="pass20")
        self.s.ensure_user(10, name="Водитель", username="drv10")
        self.s.update_user(10, driver_verification_status="approved", status="online",
                           car_model="Toyota", car_number="A123BC19")
        self.s.add_access_hours(10, 999)

    def _make_driver(self, did=10, online=True, approved=True, access=True):
        self.s.ensure_user(did, name=f"Водитель{did}", username=f"drv{did}")
        self.s.update_user(did,
            driver_verification_status="approved" if approved else "pending",
            status="online" if online else "offline", car_model="Toyota", car_number="A123BC19")
        if access:
            self.s.add_access_hours(did, 999)
        else:
            self.s.update_user(did, access_expires=None)
        return did

    def _create_pending_order(self, passenger_id=20, price=150, distance=3.0,
                          lat1=53.71, lon1=91.43, lat2=53.72, lon2=91.44):
        return self.s.create_order({
            "passenger_id": passenger_id, "pickup": "Абакан, Ленина 1",
            "destination": "Абакан, Мира 5", "pickup_address": "Абакан, Ленина 1",
            "dropoff_address": "Абакан, Мира 5",
            "pickup_coords": {"lat": lat1, "lon": lon1},
            "destination_coords": {"lat": lat2, "lon": lon2},
            "distance": distance, "time_minutes": 10, "price": price,
            "pickup_city": "Абакан", "destination_city": "Абакан",
            "status": "pending", "created_at": "2024-01-01T00:00:00",
        })

    def _fsm(self, uid):
        from core.fsm import get_context
        return get_context(uid)

    async def test_passenger_register_name_and_phone(self):
        fsm = self._fsm(20)
        await fsm.set_state("passenger:register_name")
        await self.router._handle_message(20, _make_update(text="Иван", actor=20))
        self.assertEqual(await fsm.get_state(), "passenger:register_phone")
        self.assertEqual(self.s.get_user(20).get("name"), "Иван")
        await fsm.set_state("passenger:register_phone")
        await self.router._handle_message(20, _make_update(text="+79001234567", actor=20))
        self.assertIsNone(await fsm.get_state())
        self.assertEqual(self.s.get_user(20).get("phone"), "79001234567")

    async def test_passenger_create_order_flow(self):
        self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["passenger_id"], 20)
        await self.passenger_mod.notify_drivers_about_order(self.client, oid)
        self.assertTrue(self.client.send_message.called)
        texts = _calls_texts(self.client)
        self.assertTrue(any(f"Новый заказ #{oid}" in t for t in texts))

    async def test_passenger_order_notified_only_online_drivers(self):
        self._make_driver(10, online=True, approved=True, access=True)
        self._make_driver(11, online=False, approved=True, access=True)
        self._make_driver(12, online=True, approved=False, access=True)
        oid = self._create_pending_order()
        await self.passenger_mod.notify_drivers_about_order(self.client, oid)
        driver_calls = [c for c in self.client.send_message.call_args_list
                        if c.kwargs.get("user_id") in (10, 11, 12)]
        user_ids = [c.kwargs.get("user_id") for c in driver_calls]
        self.assertIn(10, user_ids)
        self.assertNotIn(11, user_ids)
        self.assertNotIn(12, user_ids)

    async def test_passenger_no_duplicate_notification(self):
        self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.passenger_mod.notify_drivers_about_order(self.client, oid)
        before = len(self.client.send_message.call_args_list)
        await self.passenger_mod.notify_drivers_about_order(self.client, oid)
        self.assertEqual(len(self.client.send_message.call_args_list), before)

    async def test_passenger_cancel_pending_order(self):
        oid = self._create_pending_order()
        await self.router.callback_handlers["passenger:cancel:"](
            self.client, 20, _make_update(f"passenger:cancel:{oid}", 20), {})
        self.assertEqual(self.s.get_order(oid)["status"], "cancelled")

    async def test_passenger_cannot_cancel_completed_order(self):
        oid = self._create_pending_order()
        self.s.update_order(oid, status="completed")
        await self.router.callback_handlers["passenger:cancel:"](
            self.client, 20, _make_update(f"passenger:cancel:{oid}", 20), {})
        self.assertEqual(self.s.get_order(oid)["status"], "completed")

    async def test_passenger_propose_own_price(self):
        oid = self._create_pending_order()
        fsm = self._fsm(20)
        await fsm.set_state("passenger:await_offer_price")
        await fsm.update_data(stage="offer_price", offer_order_id=oid)
        await self.router._handle_message(20, _make_update(text="200", actor=20))
        self.assertEqual(self.s.get_order(oid)["price"], 200)

    async def test_passenger_favorite_add_remove(self):
        oid = self._create_pending_order()
        self.s.update_order(oid, status="completed")
        self.s.add_favorite(20, oid, label="Дом -> Работа")
        favs = self.s.get_favorites(20)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["label"], "Дом -> Работа")
        self.assertTrue(self.s.remove_favorite(20, oid))
        self.assertEqual(len(self.s.get_favorites(20)), 0)
        self.assertFalse(self.s.remove_favorite(20, oid))

    async def test_passenger_use_favorite_creates_order(self):
        oid = self._create_pending_order()
        self.s.update_order(oid, status="completed")
        self.s.add_favorite(20, oid, label="Дом -> Работа")
        with patch.object(self.passenger_mod, "notify_drivers_about_order", new=AsyncMock()):
            await self.router.callback_handlers["passenger:favorite_use:"](
                self.client, 20, _make_update("passenger:favorite_use:0", 20), {})
        new_orders = [o for o in self.s.orders.values()
                      if o.get("passenger_id") == 20 and o.get("id") != oid]
        self.assertEqual(len(new_orders), 1)
        self.assertEqual(new_orders[0]["pickup"], "Абакан, Ленина 1")

    async def test_passenger_rate_driver(self):
        oid = self._create_pending_order()
        self.s.update_order(oid, status="completed")
        await self.router.callback_handlers["passenger:rate:"](
            self.client, 20, _make_update(f"passenger:rate:{oid}:5", 20), {})
        self.assertEqual(self.s.get_order_rating(oid), 5)
        texts = _calls_texts(self.client)
        self.assertTrue(any("5/5" in t for t in texts))

    async def test_passenger_cannot_rate_others_order(self):
        oid = self._create_pending_order(passenger_id=20)
        self.s.update_order(oid, status="completed")
        await self.router.callback_handlers["passenger:rate:"](
            self.client, 99, _make_update(f"passenger:rate:{oid}:1", 99), {})
        self.assertIsNone(self.s.get_order_rating(oid))

    async def test_passenger_waypoint_add(self):
        oid = self._create_pending_order()
        fsm = self._fsm(20)
        await fsm.set_state("passenger:await_waypoint")
        loc = SimpleNamespace(latitude=53.715, longitude=91.435)
        await self.router._handle_message(20, _make_update(location=loc, actor=20))
        self.assertIsNotNone((await self._fsm(20).get_data()).get("waypoints"))

    async def test_driver_registration_flow(self):
        did = 30
        self.s.ensure_user(did, name="Новый Водитель", username="drv30")
        await self.driver_mod.driver_start_registration(
            self.client, did, _make_update("driver:start_reg", did), {})
        fsm = self._fsm(did)
        self.assertEqual(await fsm.get_state(), "driver:reg_name")
        await self.driver_mod.reg_name(
            self.client, did, _make_update(text="Пётр", actor=did), {})
        self.assertEqual(await fsm.get_state(), "driver:reg_car_model")
        from handlers.driver import process_edit_car_model
        await process_edit_car_model(
            self.client, did, _make_update(text="Toyota", actor=did), {})
        self.assertEqual(await fsm.get_state(), "driver:reg_car_number")
        from handlers.driver import process_edit_car_number
        await process_edit_car_number(
            self.client, did, _make_update(text="A123BC19", actor=did), {})
        self.assertEqual(await fsm.get_state(), "driver:reg_car_color")
        from handlers.driver import process_edit_car_color
        await process_edit_car_color(
            self.client, did, _make_update(text="Красный", actor=did), {})
        self.assertIsNone(await fsm.get_state())
        self.assertTrue(self.s.is_driver_profile_complete(did))
        u = self.s.get_user(did)
        self.assertEqual(u.get("name"), "Пётр")
        self.assertEqual(u.get("car_model"), "Toyota")
        self.assertEqual(u.get("car_number"), "A123BC19")
        self.assertEqual(u.get("car_color"), "Красный")

    async def test_driver_moderation_pending(self):
        did = self._make_driver(40, online=False, approved=False, access=False)
        # Водитель на модерации может выйти на линию (статус меняется),
        # но не может принять заказ
        await self.driver_mod.driver_set_status(
            self.client, did, _make_update("driver:online", did), {})
        self.assertEqual(self.s.get_user(did).get("status"), "online")
        # Но принять заказ не может
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        self.assertEqual(self.s.get_order(oid)["status"], "pending")

    async def test_driver_go_online(self):
        did = self._make_driver(10, online=False, approved=True, access=True)
        await self.driver_mod.driver_set_status(
            self.client, did, _make_update("driver:online", did), {})
        self.assertEqual(self.s.get_user(did).get("status"), "online")

    async def test_driver_go_break(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_set_status(
            self.client, did, _make_update("driver:break", did), {})
        self.assertEqual(self.s.get_user(did).get("status"), "break")

    async def test_driver_go_offline(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_set_status(
            self.client, did, _make_update("driver:offline", did), {})
        self.assertEqual(self.s.get_user(did).get("status"), "offline")

    async def test_driver_cannot_go_online_without_access(self):
        self.s.set_commission(enabled=True, mode="access", percent=0,
                              access_price=1000, access_days=7)
        did = self._make_driver(999, online=False, approved=True, access=False)
        await self.driver_mod.driver_set_status(
            self.client, did, _make_update("driver:online", did), {})
        self.assertEqual(self.s.get_user(did).get("status"), "offline")
        self.s.set_commission(enabled=False, mode="percent", percent=0,
                              access_price=1000, access_days=7)

    async def test_driver_blocked_cannot_change_status(self):
        did = self._make_driver(10, online=False, approved=True, access=True)
        for _ in range(3):
            self.s.record_driver_cancel(did)
        self.assertTrue(self.s.is_driver_blocked(did))
        await self.driver_mod.driver_set_status(
            self.client, did, _make_update("driver:online", did), {})
        self.assertEqual(self.s.get_user(did).get("status"), "offline")

    def test_haversine_distance_calculation(self):
        d = haversine_distance(55.7558, 37.6173, 59.9343, 30.3351)
        self.assertGreater(d, 600)
        self.assertLess(d, 700)
        d0 = haversine_distance(53.71, 91.43, 53.71, 91.43)
        self.assertAlmostEqual(d0, 0.0, places=5)

    def test_get_available_orders_sorted_by_distance(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        self.s.update_user(10, last_lat=53.71, last_lon=91.43)
        oid_far = self._create_pending_order(price=200, distance=10.0,
                                              lat1=53.80, lon1=91.50)
        oid_near = self._create_pending_order(price=100, distance=1.0,
                                               lat1=53.715, lon1=91.435)
        orders = get_available_orders_for_driver(10, ref_lat=53.71, ref_lon=91.43)
        if orders and isinstance(orders[0], tuple):
            self.assertLessEqual(orders[0][1], orders[-1][1])
        else:
            ids = [o.get("id") for o in orders]
            self.assertIn(oid_near, ids)
            self.assertIn(oid_far, ids)

    async def test_driver_view_available_orders(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod.driver_orders_available(
            self.client, did, _make_update("driver:orders_available", did), {})
        self.assertTrue(self.client.send_message.called)
        fsm = self._fsm(did)
        self.assertEqual(await fsm.get_state(), self.driver_mod.STATE_ORDER_LIST_VIEW)

    async def test_driver_no_available_orders(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_orders_available(
            self.client, did, _make_update("driver:orders_available", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_accept_order_success(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "accepted")
        self.assertEqual(order["driver_id"], did)
        self.assertEqual(self.s.get_user(did).get("driver_current_order_id"), oid)

    async def test_driver_cannot_accept_while_offline(self):
        did = self._make_driver(10, online=False, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "pending")

    async def test_driver_cannot_accept_while_busy(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid1 = self._create_pending_order()
        oid2 = self._create_pending_order(passenger_id=21)
        await self.driver_mod._accept_order(self.client, did, oid1)
        await self.driver_mod._accept_order(self.client, did, oid2)
        order2 = self.s.get_order(oid2)
        self.assertEqual(order2["status"], "pending")

    async def test_driver_accept_already_taken_order(self):
        did1 = self._make_driver(10, online=True, approved=True, access=True)
        did2 = self._make_driver(11, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did1, oid)
        await self.driver_mod._accept_order(self.client, did2, oid)
        order = self.s.get_order(oid)
        self.assertEqual(order["driver_id"], did1)

    async def test_driver_passenger_notified_on_accept(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        passenger_notified = any(
            c.kwargs.get("user_id") == 20
            for c in self.client.send_message.call_args_list)
        self.assertTrue(passenger_notified)

    async def test_driver_active_order_card(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_current_order(
            self.client, did, _make_update("driver:current_order", did), {})
        self.assertTrue(self.client.send_message.called)
        texts = _calls_texts(self.client)
        card_text = texts[-1]
        self.assertIn(f"Заказ #{oid}", card_text)
        self.assertIn("Абакан, Ленина 1", card_text)
        self.assertIn("Абакан, Мира 5", card_text)
        self.assertIn("₽", card_text)

    async def test_driver_no_active_order(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_current_order(
            self.client, did, _make_update("driver:current_order", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_start_driving(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_start_driving(
            self.client, did, _make_update(f"driver:start_driving:{oid}", did), {})
        self.assertEqual(self.s.get_order(oid)["status"], "in_progress")

    async def test_driver_report_waiting_arrived(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_report_waiting(
            self.client, did, _make_update(f"driver:report_waiting:{oid}", did), {})
        self.assertEqual(self.s.get_order(oid)["status"], "driver_arrived")

    async def test_driver_wait_start_stop(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_report_waiting(
            self.client, did, _make_update(f"driver:report_waiting:{oid}", did), {})
        await self.driver_mod.driver_wait_start(
            self.client, did, _make_update(f"driver:wait_start:{oid}", did), {})
        self.assertIsNotNone(self.s.get_order(oid).get("waiting_started_at"))
        await self.driver_mod.driver_wait_stop(
            self.client, did, _make_update(f"driver:wait_stop:{oid}", did), {})
        self.assertIsNotNone(self.s.get_order(oid).get("waiting_ended_at"))

    async def test_driver_complete_trip_flow(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order(
            lat1=53.71, lon1=91.43, lat2=53.72, lon2=91.44,
        )
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_start_driving(
            self.client, did, _make_update(f"driver:start_driving:{oid}", did), {})
        self.assertEqual(self.s.get_order(oid)["status"], "in_progress")
        await self.driver_mod.driver_complete_trip(
            self.client, did, _make_update(f"driver:complete_trip:{oid}", did), {})
        fsm = self._fsm(did)
        self.assertEqual(await fsm.get_state(), "driver:await_final_geo")
        loc = SimpleNamespace(latitude=53.725, longitude=91.445)
        await self.driver_mod.driver_trip_final_geo(
            self.client, did, _make_update(location=loc, actor=did), {})
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "completed")
        self.assertIsNotNone(order.get("completed_at"))
        self.assertGreater(order.get("final_price", 0), 0)
        self.assertGreater(order.get("driver_earning", 0), 0)
        self.assertEqual(await fsm.get_state(), self.driver_mod.STATE_IDLE)

    async def test_driver_complete_trip_invalid_status(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_complete_trip(
            self.client, did, _make_update(f"driver:complete_trip:{oid}", did), {})
        self.assertNotEqual(self.s.get_order(oid)["status"], "completed")

    async def test_driver_complete_trip_wrong_driver(self):
        did1 = self._make_driver(10, online=True, approved=True, access=True)
        did2 = self._make_driver(11, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did1, oid)
        await self.driver_mod.driver_start_driving(
            self.client, did1, _make_update(f"driver:start_driving:{oid}", did1), {})
        await self.driver_mod.driver_complete_trip(
            self.client, did2, _make_update(f"driver:complete_trip:{oid}", did2), {})
        self.assertNotEqual(self.s.get_order(oid)["status"], "completed")

    async def test_driver_final_geo_invalid_location(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_start_driving(
            self.client, did, _make_update(f"driver:start_driving:{oid}", did), {})
        await self.driver_mod.driver_complete_trip(
            self.client, did, _make_update(f"driver:complete_trip:{oid}", did), {})
        await self.driver_mod.driver_trip_final_geo(
            self.client, did, _make_update(text="текст без гео", actor=did), {})
        self.assertEqual(self.s.get_order(oid)["status"], "in_progress")

    async def test_driver_cancel_accepted_order(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_cancel_order(
            self.client, did, _make_update(f"driver:cancel_order:{oid}", did), {})
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order.get("cancelled_by"), "driver")
        self.assertIsNone(self.s.get_user(did).get("driver_current_order_id"))

    async def test_driver_cancel_blocks_after_threshold(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        for i in range(3):
            oid = self._create_pending_order(passenger_id=20 + i)
            await self.driver_mod._accept_order(self.client, did, oid)
            await self.driver_mod.driver_cancel_order(
                self.client, did, _make_update(f"driver:cancel_order:{oid}", did), {})
        self.assertTrue(self.s.is_driver_blocked(did))

    async def test_driver_cannot_cancel_completed_order(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        self.s.update_order(oid, status="completed")
        await self.driver_mod.driver_cancel_order(
            self.client, did, _make_update(f"driver:cancel_order:{oid}", did), {})
        self.assertEqual(self.s.get_order(oid)["status"], "completed")

    async def test_driver_yandex_navigation(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_yandex_nav(
            self.client, did, _make_update(f"driver:yandex_nav:{oid}", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_contact_passenger(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        await self.driver_mod.driver_contact_passenger(
            self.client, did, _make_update(f"driver:contact:{oid}", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_view_balance(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        self.s.add_balance(did, 500)
        await self.driver_mod.driver_balance(
            self.client, did, _make_update("driver:balance", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_buy_access(self):
        did = self._make_driver(10, online=True, approved=True, access=False)
        self.s.set_commission(enabled=True, mode="access")
        self.s.add_balance(did, 1000)
        await self.driver_mod.driver_balance_buy_access(
            self.client, did, _make_update("driver:balance:buy:2", did), {})
        self.assertIsNotNone(self.s.get_access_expires(did))

    async def test_driver_buy_access_insufficient_balance(self):
        did = self._make_driver(10, online=True, approved=True, access=False)
        self.s.set_commission(enabled=True, mode="access")
        self.s.add_balance(did, 10)
        await self.driver_mod.driver_balance_buy_access(
            self.client, did, _make_update("driver:balance:buy:2", did), {})
        self.assertIsNone(self.s.get_access_expires(did))

    async def test_driver_balance_history(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        self.s.add_transaction(did, "earning", 100, description="тест")
        await self.driver_mod.driver_balance_history(
            self.client, did, _make_update("driver:balance:history", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_statistics_day(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_stats_day(
            self.client, did, _make_update("driver:stats:day", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_statistics_week(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_stats_week(
            self.client, did, _make_update("driver:stats:week", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_statistics_month(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_stats_month(
            self.client, did, _make_update("driver:stats:month", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_statistics_general(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_stats_general(
            self.client, did, _make_update("driver:stats:general", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_statistics_custom(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        fsm = self._fsm(did)
        await fsm.clear()
        await self.driver_mod.driver_stats_custom(
            self.client, did, _make_update("driver:stats:custom", did), {})
        self.assertEqual(await fsm.get_state(), "driver:stats_custom")

    async def test_driver_view_profile(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_profile(
            self.client, did, _make_update("driver:profile", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_edit_profile_fields(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        from handlers.driver import process_edit_name
        await process_edit_name(
            self.client, did, _make_update(text="Иван Updated", actor=did), {})
        self.assertEqual(self.s.get_user(did).get("driver_name"), "Иван Updated")
        from handlers.driver import process_edit_phone
        await process_edit_phone(
            self.client, did, _make_update(text="+79001112233", actor=did), {})
        self.assertEqual(self.s.get_user(did).get("driver_phone"), "+79001112233")

    async def test_driver_upload_documents(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        img = SimpleNamespace(image_token="token-123")
        await self.driver_mod.driver_doc_upload_selfie(
            self.client, did, _make_update("driver:docs:selfie", did), {})
        fsm = self._fsm(did)
        self.assertEqual(await fsm.get_state(), "driver:upload_selfie")
        update = _make_update(image=img, actor=did)
        await self.router._handle_message(did, update)
        self.assertEqual(self.s.get_user(did).get("selfie_photo_token"), "token-123")

    async def test_driver_orders_history(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        oid = self._create_pending_order()
        self.s.update_order(oid, status="completed", driver_id=did,
                            completed_at="2024-01-01T00:00:00")
        await self.driver_mod.driver_orders_history(
            self.client, did, _make_update("driver:orders_history", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_help(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        await self.driver_mod.driver_help(
            self.client, did, _make_update("driver:help", did), {})
        self.assertTrue(self.client.send_message.called)

    async def test_driver_relay_session(self):
        did = self._make_driver(10, online=True, approved=True, access=True)
        pid = 20
        oid = self._create_pending_order()
        await self.driver_mod._accept_order(self.client, did, oid)
        from core.driver_support import start_relay_session, STATE_RELAY
        start_relay_session(did, pid, oid, hours=1.0)
        fsm = self._fsm(did)
        await fsm.set_state(STATE_RELAY)
        from handlers.driver import driver_relay_message
        await driver_relay_message(self.client, did, _make_update(text="Здравствуйте", actor=did), {})
        passenger_calls = [c for c in self.client.send_message.call_args_list
                           if c.kwargs.get("user_id") == pid]
        self.assertTrue(len(passenger_calls) > 0)

    async def test_full_cycle_passenger_and_driver(self):
        oid = self._create_pending_order()
        did = self._make_driver(10, online=False, approved=True, access=True)
        await self.driver_mod.driver_set_status(
            self.client, did, _make_update("driver:online", did), {})
        self.assertEqual(self.s.get_user(did).get("status"), "online")
        await self.driver_mod.driver_orders_available(
            self.client, did, _make_update("driver:orders_available", did), {})
        await self.driver_mod._accept_order(self.client, did, oid)
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "accepted")
        self.assertEqual(order["driver_id"], did)
        passenger_notified = any(
            c.kwargs.get("user_id") == 20
            for c in self.client.send_message.call_args_list)
        self.assertTrue(passenger_notified)
        await self.driver_mod.driver_current_order(
            self.client, did, _make_update("driver:current_order", did), {})
        await self.driver_mod.driver_start_driving(
            self.client, did, _make_update(f"driver:start_driving:{oid}", did), {})
        self.assertEqual(self.s.get_order(oid)["status"], "in_progress")
        await self.driver_mod.driver_report_waiting(
            self.client, did, _make_update(f"driver:report_waiting:{oid}", did), {})
        self.assertEqual(self.s.get_order(oid)["status"], "driver_arrived")
        await self.driver_mod.driver_wait_start(
            self.client, did, _make_update(f"driver:wait_start:{oid}", did), {})
        await self.driver_mod.driver_wait_stop(
            self.client, did, _make_update(f"driver:wait_stop:{oid}", did), {})
        await self.driver_mod.driver_complete_trip(
            self.client, did, _make_update(f"driver:complete_trip:{oid}", did), {})
        loc = SimpleNamespace(latitude=53.725, longitude=91.445)
        await self.driver_mod.driver_trip_final_geo(
            self.client, did, _make_update(location=loc, actor=did), {})
        order = self.s.get_order(oid)
        self.assertEqual(order["status"], "completed")
        self.assertGreater(order.get("driver_earning", 0), 0)
        await self.router.callback_handlers["passenger:rate:"](
            self.client, 20, _make_update(f"passenger:rate:{oid}:5", 20), {})
        self.assertEqual(self.s.get_order_rating(oid), 5)
        fsm = self._fsm(did)
        self.assertEqual(await fsm.get_state(), self.driver_mod.STATE_IDLE)


if __name__ == "__main__":
    unittest.main()

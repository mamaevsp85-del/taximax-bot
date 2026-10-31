"""Фото автомобиля при регистрации водителя.

Проверяем:
  - заявка без фото не уходит (driver.apply из Mini-App отклоняется);
  - достаточно фото, приложенного ранее (car_photo_path / car_photo_token);
  - хранение mid уведомлений админам, чтобы после «Одобрить/Отклонить»
    кнопки решения исчезали из того же сообщения.
"""
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DriverPhotoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "photo.json"),
            "MAX_BOT_TOKEN": "offline-test-token",
            "ADMIN_USER_IDS": "99",
            "API_KEY": "test-api-key",
        })
        cls.env.start()
        cls.storage_module = importlib.import_module("core.storage")
        cls.maximus = importlib.import_module("maximus_server")

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.s.ensure_user(10, name="Иван", username="ivan10")
        self.s.update_user(10, phone="+79130000000")
        self._orig = self.maximus.S
        self.maximus.S = self.s

    def tearDown(self):
        self.maximus.S = self._orig

    def test_apply_without_photo_rejected(self):
        """Заявка без фото авто отклоняется с понятной ошибкой."""
        with self.assertRaises(ValueError) as ctx:
            self.maximus.act(10, {"op": "driver.apply",
                                  "car": "Kia Rio", "plate": "А123ВС 19", "color": "белый"})
        self.assertIn("фото", str(ctx.exception).lower())

    def test_apply_with_uploaded_photo_ok(self):
        """Фото, загруженное через /api/photo (car_photo_path), достаточно для заявки."""
        self.s.update_user(10, car_photo_path="data/photos/10.jpg")
        result = self.maximus.act(10, {"op": "driver.apply",
                                       "car": "Kia Rio", "plate": "А123ВС 19", "color": "белый"})
        self.assertIs(result.get("ok"), True)
        u = self.s.get_user(10)
        self.assertEqual(u.get("driver_verification_status"), "pending")
        self.assertEqual(u.get("car_photo_path"), "data/photos/10.jpg")

    def test_apply_with_bot_photo_token_ok(self):
        """Фото, присланное в чат бота (car_photo_token), тоже засчитывается."""
        self.s.update_user(10, car_photo_token="abc-token-from-max")
        result = self.maximus.act(10, {"op": "driver.apply",
                                       "car": "Лада Гранта", "plate": "В234КМ 24", "color": "серая"})
        self.assertIs(result.get("ok"), True)
        u = self.s.get_user(10)
        self.assertEqual(u.get("car_photo_token"), "abc-token-from-max")

    def test_submit_saves_photo_path(self):
        self.s.submit_driver_application(
            10, car_number="А123ВС 19", car_color="белый", car_photo_path="data/photos/10.jpg")
        app = self.s.get_driver_application(10)
        self.assertEqual(app["car_photo_path"], "data/photos/10.jpg")
        self.assertEqual(app["status"], "pending")

    def test_notify_mids_roundtrip(self):
        """mid уведомления админу сохраняется и чистится после решения."""
        self.s.set_driver_notify_mid(10, 99, "mid-123")
        mids = self.s.get_driver_notify_mids(10)
        self.assertEqual(mids.get(99), "mid-123")
        self.s.clear_driver_notify_mids(10)
        self.assertEqual(self.s.get_driver_notify_mids(10), {})

    def test_get_all_drivers_car_photo_flag(self):
        self.s.update_user(10, car_photo_path="data/photos/10.jpg")
        self.s.approve_driver(10)
        drivers = self.s.get_all_drivers()
        row = next(d for d in drivers if d["user_id"] == 10)
        self.assertTrue(row["car_photo"])


if __name__ == "__main__":
    unittest.main()
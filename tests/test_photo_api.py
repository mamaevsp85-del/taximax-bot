"""Эндпоинты фото автомобиля: POST /api/photo и GET /api/photo/{uid}.

Прогоняем на aiohttp TestClient с временным каталогом и временной базой,
чтобы не трогать рабочие данные.
"""
import asyncio
import base64
import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

# 1x1 прозрачный PNG
PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082")).decode()


class PhotoApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            "STORAGE_FILE": str(Path(cls.temp.name) / "photoapi.json"),
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
        self.photos = tempfile.mkdtemp(dir=self.temp.name)
        self._orig_dir = self.api.PHOTOS_DIR
        self.api.PHOTOS_DIR = self.photos
        path = str(Path(self.temp.name) / f"{self._testMethodName}.json")
        self.s = self.storage_module.Storage(path)
        self.s.ensure_user(30, name="Водитель")
        self._orig_storage = self.api.storage.storage
        self.api.storage.storage = self.s
        self.api.maximus_server.S = self.s

        async def _start():
            app = self.api.create_app()
            client = TestClient(TestServer(app))
            await client.start_server()
            return client

        self.loop = asyncio.new_event_loop()
        self.client = self.loop.run_until_complete(_start())

    def tearDown(self):
        async def _close():
            await self.client.close()
        self.loop.run_until_complete(_close())
        self.loop.close()
        self.api.PHOTOS_DIR = self._orig_dir
        self.api.storage.storage = self._orig_storage
        shutil.rmtree(self.photos, ignore_errors=True)

    def _post(self, payload):
        async def _go():
            async with self.client.post("/api/photo", json=payload) as r:
                return r.status, await r.json()
        return self.loop.run_until_complete(_go())

    def _get(self, uid):
        async def _go():
            async with self.client.get(f"/api/photo/{uid}") as r:
                return r.status, await r.read()
        return self.loop.run_until_complete(_go())

    def test_upload_and_fetch(self):
        status, body = self._post({"uid": 30, "file": "data:image/png;base64," + PNG})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"))
        # сохранилось в профиле пользователя
        self.assertEqual(self.s.get_user(30).get("car_photo_path"), "data/photos/30.jpg")
        st, data = self._get(30)
        self.assertEqual(st, 200)
        self.assertGreater(len(data), 10)

    def test_upload_requires_auth(self):
        status, body = self._post({"file": "data:image/png;base64," + PNG})
        self.assertEqual(status, 403)

    def test_upload_requires_file(self):
        status, body = self._post({"uid": 30})
        self.assertEqual(status, 400)

    def test_missing_photo_returns_404(self):
        st, _ = self._get(777)
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
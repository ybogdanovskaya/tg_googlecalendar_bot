from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.miniapp_api import ApiError, create_app, validate_telegram_init_data
from app.miniapp_services import MiniAppBookingService


class FreeCalendar:
    async def busy(self, start_at, end_at):
        return []

    async def is_free(self, start_at, end_at):
        return True


class MiniAppApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(
            bot_token="123:test-token",
            admin_telegram_id=1,
            google_calendar_id="primary",
            google_token_file=Path("unused.json"),
            apps_script_url="",
            apps_script_secret_file=Path("unused.txt"),
            database_path=Path(self.temporary.name) / "miniapp.sqlite3",
            log_path=Path(self.temporary.name) / "miniapp.jsonl",
            timezone="Europe/Moscow",
            min_lead_minutes=120,
            booking_horizon_days=30,
            hold_hours=24,
            privacy_policy_version="test-v1",
        )
        self.database = Database(self.settings.database_path)
        self.database.initialize()
        self.database.upsert_user(100, "Тест", "tester")
        self.database.set_consent(100, "test-v1")
        self.service = MiniAppBookingService(self.settings, self.database, FreeCalendar())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _signed_init_data(self, telegram_id: int = 100) -> str:
        values = {
            "auth_date": str(int(time.time())),
            "query_id": "query",
            "user": json.dumps({"id": telegram_id, "first_name": "Тест", "username": "tester"}, separators=(",", ":")),
        }
        data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", self.settings.bot_token.encode("utf-8"), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
        return urlencode(values)

    def test_validates_signed_telegram_init_data(self) -> None:
        user = validate_telegram_init_data(self._signed_init_data(), self.settings.bot_token)
        self.assertEqual(user["id"], 100)

    def test_rejects_invalid_telegram_signature(self) -> None:
        with self.assertRaises(ApiError) as captured:
            validate_telegram_init_data("auth_date=1&user=%7B%7D&hash=invalid", self.settings.bot_token)
        self.assertEqual(captured.exception.code, "AUTH_INVALID")

    async def test_create_request_is_idempotent(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=3), clock_time(10, 0), zone)
        first, replayed = await self.service.create_request(
            telegram_id=100,
            telegram_name="Тест",
            telegram_username="tester",
            name="Тест",
            email="test@example.com",
            subject="Проверка",
            description=None,
            location=None,
            start_at=start,
            duration_minutes=30,
            idempotency_key="a" * 24,
        )
        second, replayed_second = await self.service.create_request(
            telegram_id=100,
            telegram_name="Тест",
            telegram_username="tester",
            name="Тест",
            email="test@example.com",
            subject="Проверка",
            description=None,
            location=None,
            start_at=start,
            duration_minutes=30,
            idempotency_key="a" * 24,
        )
        self.assertFalse(replayed)
        self.assertTrue(replayed_second)
        self.assertEqual(first.id, second.id)

    def test_http_authentication_session_and_csrf(self) -> None:
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            auth = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data()})
            self.assertEqual(auth.status_code, 200)
            csrf_token = auth.json()["csrf_token"]
            self.assertEqual(client.get("/api/v1/me").status_code, 200)

            denied = client.post(
                "/api/v1/consents",
                json={"accepted": True},
                headers={"Idempotency-Key": "b" * 24},
            )
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["error"]["code"], "CSRF_INVALID")

            accepted = client.post(
                "/api/v1/consents",
                json={"accepted": True},
                headers={"Idempotency-Key": "c" * 24, "X-CSRF-Token": csrf_token},
            )
            self.assertEqual(accepted.status_code, 200)
            self.assertTrue(accepted.json()["accepted"])

            wrong_origin = client.post(
                "/api/v1/consents",
                json={"accepted": True},
                headers={"Idempotency-Key": "d" * 24, "X-CSRF-Token": csrf_token, "Origin": "https://example.test"},
            )
            self.assertEqual(wrong_origin.status_code, 403)
            self.assertEqual(wrong_origin.json()["error"]["code"], "CSRF_INVALID")

    def test_http_does_not_expose_another_users_request(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=3), clock_time(10, 0), zone).astimezone(ZoneInfo("UTC"))
        created = self.database.create_request(
            telegram_id=100,
            telegram_name="Тест",
            telegram_username="tester",
            email="test@example.com",
            subject="Личная заявка",
            description=None,
            location=None,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            hold_hours=24,
        )
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            auth = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(200)})
            self.assertEqual(auth.status_code, 200)
            response = client.get(f"/api/v1/requests/{created.id}")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()

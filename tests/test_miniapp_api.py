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
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.miniapp_api import ApiError, create_app, load_local_env, validate_telegram_init_data
from app.miniapp_services import BookingValidationError, MiniAppBookingService
from app.models import CHANGE_CANCEL
from app.release_d_store import DELETE_COMPLETED, DELETE_KEEP_FUTURE


class FreeCalendar:
    async def busy(self, start_at, end_at):
        return []

    async def is_free(self, start_at, end_at):
        return True

    async def create_event(self, request):
        return f"test-event-{request.id}"

    async def create_series(self, series):
        return f"test-series-{series.id}"

    async def delete_series(self, series_id):
        return True

    async def delete_event(self, event_id):
        return True

    async def update_event(self, request):
        return request.google_event_id

    async def delete_occurrence(self, series_id, occurrence):
        return True

    async def update_occurrence(self, series_id, occurrence, start_at, end_at):
        return f"test-occurrence-{occurrence.id}"


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
            booking_horizon_days=90,
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

    def test_local_env_loader_ignores_inaccessible_developer_file(self) -> None:
        with patch.object(Path, "exists", side_effect=PermissionError):
            load_local_env(Path(".env"))

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

    def test_health_requires_no_authentication(self) -> None:
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            response = client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

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

    async def test_request_rejects_email_instead_of_name(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=3), clock_time(11, 0), zone)

        with self.assertRaisesRegex(BookingValidationError, "invalid name"):
            await self.service.create_request(
                telegram_id=100,
                telegram_name="Tester",
                telegram_username="tester",
                name="test@example.com",
                email="test@example.com",
                subject="Проверка",
                description=None,
                location=None,
                start_at=start,
                duration_minutes=30,
                idempotency_key="z" * 24,
            )

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

    def test_user_requests_separate_past_records_into_archive(self) -> None:
        now = datetime.now(ZoneInfo("UTC"))
        active = self.database.create_request(
            telegram_id=100, telegram_name="Tester", telegram_username="tester", email="test@example.com",
            subject="Будущая встреча", description=None, location=None,
            start_at=now + timedelta(days=3), end_at=now + timedelta(days=3, minutes=30), hold_hours=24,
        )
        archived = self.database.create_request(
            telegram_id=100, telegram_name="Tester", telegram_username="tester", email="test@example.com",
            subject="Прошедшая встреча", description=None, location=None,
            start_at=now - timedelta(days=2), end_at=now - timedelta(days=2, minutes=30), hold_hours=24,
        )
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(100)})
            payload = client.get("/api/v1/requests").json()
        self.assertEqual([item["id"] for item in payload["items"]], [str(active.id)])
        self.assertEqual([item["id"] for item in payload["archive"]], [str(archived.id)])

    def test_user_requests_show_latest_created_first(self) -> None:
        now = datetime.now(ZoneInfo("UTC"))
        first = self.database.create_request(
            telegram_id=100, telegram_name="Tester", telegram_username="tester", email="test@example.com",
            subject="First", description=None, location=None,
            start_at=now + timedelta(days=5), end_at=now + timedelta(days=5, minutes=30), hold_hours=24,
        )
        latest = self.database.create_request(
            telegram_id=100, telegram_name="Tester", telegram_username="tester", email="test@example.com",
            subject="Latest", description=None, location=None,
            start_at=now + timedelta(days=2), end_at=now + timedelta(days=2, minutes=30), hold_hours=24,
        )
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(100)})
            payload = client.get("/api/v1/requests").json()
        self.assertEqual([item["id"] for item in payload["items"]], [str(latest.id), str(first.id)])

    def test_calendar_allows_the_confirmed_ninety_day_horizon(self) -> None:
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(100)})
            config = client.get("/api/v1/booking/config")
            allowed = client.get(
                "/api/v1/booking/calendar",
                params={"from_date": today.isoformat(), "to_date": (today + timedelta(days=89)).isoformat()},
            )
            too_long = client.get(
                "/api/v1/booking/calendar",
                params={"from_date": today.isoformat(), "to_date": (today + timedelta(days=90)).isoformat()},
            )
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.json()["hold_hours"], 24)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(too_long.status_code, 422)

    def test_admin_routes_require_server_side_admin_role(self) -> None:
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as user_client:
            user_client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(100)})
            denied = user_client.get("/api/v1/admin/dashboard")
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["error"]["code"], "ACCESS_DENIED")
        with TestClient(app) as admin_client:
            auth = admin_client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(1)})
            self.assertEqual(auth.status_code, 200)
            dashboard = admin_client.get("/api/v1/admin/dashboard")
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn("pending_requests", dashboard.json())
            self.assertEqual(admin_client.get("/api/v1/admin/requests").status_code, 200)
            statistics = admin_client.get("/api/v1/admin/statistics?from_date=2026-01-01&to_date=2026-01-31")
            self.assertEqual(statistics.status_code, 200)
            self.assertEqual(statistics.json()["from_date"], "2026-01-01")
            self.assertEqual(admin_client.get("/api/v1/admin/integration/calendar").json()["status"], "OK")

    def test_admin_can_approve_or_reject_pending_request(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=3), clock_time(10, 0), zone).astimezone(ZoneInfo("UTC"))
        first = self.database.create_request(
            telegram_id=100, telegram_name="Тест", telegram_username="tester", email="test@example.com",
            subject="Согласовать", description=None, location=None, start_at=start, end_at=start + timedelta(minutes=30), hold_hours=24,
        )
        second = self.database.create_request(
            telegram_id=100, telegram_name="Тест", telegram_username="tester", email="test@example.com",
            subject="Отклонить", description=None, location=None, start_at=start + timedelta(days=1), end_at=start + timedelta(days=1, minutes=30), hold_hours=24,
        )
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            csrf = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(1)}).json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "i" * 24}
            edited = client.patch(
                f"/api/v1/admin/requests/{first.id}",
                json={"subject": "Обновлённая тема", "description": None},
                headers={**headers, "Idempotency-Key": "w" * 24},
            )
            self.assertEqual(edited.status_code, 200)
            self.assertEqual(edited.json()["subject"], "Обновлённая тема")
            approved = client.post(f"/api/v1/admin/requests/{first.id}/approve", headers=headers)
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["status"], "APPROVED")
            rejected = client.post(f"/api/v1/admin/requests/{second.id}/reject", headers={**headers, "Idempotency-Key": "j" * 24})
            self.assertEqual(rejected.status_code, 200)
            self.assertEqual(rejected.json()["status"], "REJECTED")

    def test_admin_can_offer_alternative_and_manage_settings(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=3), clock_time(10, 0), zone).astimezone(ZoneInfo("UTC"))
        request = self.database.create_request(
            telegram_id=100,
            telegram_name="РўРµСЃС‚",
            telegram_username="tester",
            email="test@example.com",
            subject="РџРµСЂРµРЅРѕСЃ",
            description=None,
            location=None,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            hold_hours=24,
        )
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            csrf = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(1)}).json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "k" * 24}

            alternative = client.post(
                f"/api/v1/admin/requests/{request.id}/alternatives",
                json={"start_at": (start + timedelta(days=1)).isoformat(), "duration_minutes": 30},
                headers=headers,
            )
            self.assertEqual(alternative.status_code, 200)
            self.assertIn("id", alternative.json())
            self.assertEqual(alternative.json()["duration_minutes"], 30)

            settings = client.get("/api/v1/admin/settings")
            self.assertEqual(settings.status_code, 200)
            self.assertTrue(settings.json()["booking"]["booking_enabled"])
            self.assertEqual(settings.json()["booking"]["closed_weekdays"], [])
            updated = client.patch(
                "/api/v1/admin/settings",
                json={"key": "booking_enabled", "value": False},
                headers={**headers, "Idempotency-Key": "l" * 24},
            )
            self.assertEqual(updated.status_code, 200)
            self.assertFalse(updated.json()["value"])

            weekends = client.patch(
                "/api/v1/admin/settings",
                json={"key": "closed_weekdays", "value": [5, 6]},
                headers={**headers, "Idempotency-Key": "q" * 24},
            )
            self.assertEqual(weekends.status_code, 200)
            self.assertEqual(weekends.json()["value"], [5, 6])

            booking_window = client.patch(
                "/api/v1/admin/settings",
                json={"key": "user_booking_window", "value": [9 * 60, 20 * 60 + 30]},
                headers={**headers, "Idempotency-Key": "w" * 24},
            )
            self.assertEqual(booking_window.status_code, 200)
            self.assertEqual(booking_window.json()["value"], [540, 1230])

            added = client.post(
                "/api/v1/admin/closed-dates",
                json={"date": "2099-01-01"},
                headers={**headers, "Idempotency-Key": "m" * 24},
            )
            self.assertEqual(added.status_code, 200)
            self.assertTrue(added.json()["added"])
            closed_dates = client.get("/api/v1/admin/closed-dates").json()
            self.assertIn("2099-01-01", closed_dates["items"])
            self.assertEqual(closed_dates["weekdays"], [5, 6])
            removed = client.delete(
                "/api/v1/admin/closed-dates/2099-01-01",
                headers={**headers, "Idempotency-Key": "n" * 24},
            )
            self.assertEqual(removed.status_code, 204)

    def test_admin_can_create_manual_meeting(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=4), clock_time(14, 0), zone)
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            csrf = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(1)}).json()["csrf_token"]
            created = client.post(
                "/api/v1/admin/manual-meetings",
                json={
                    "subject": "Ручная встреча",
                    "email": "guest@example.com",
                    "start_at": start.isoformat(),
                    "duration_minutes": 45,
                    "blocks_calendar": True,
                    "allow_overlap": False,
                },
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "o" * 24},
            )
            self.assertEqual(created.status_code, 200)
            self.assertEqual(created.json()["status"], "APPROVED")
            self.assertEqual(created.json()["email"], "guest@example.com")
            self.assertEqual(len(client.get("/api/v1/admin/manual-meetings").json()["items"]), 1)
            edited = client.patch(
                f"/api/v1/admin/manual-meetings/{created.json()['id']}",
                json={"subject": "Обновлённая ручная встреча"},
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "x" * 24},
            )
            self.assertEqual(edited.status_code, 200)
            self.assertEqual(edited.json()["subject"], "Обновлённая ручная встреча")
            cancelled = client.post(
                f"/api/v1/admin/manual-meetings/{created.json()['id']}/cancel",
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "v" * 24},
            )
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["status"], "CANCELLED_BY_ADMIN")

    def test_admin_can_create_all_day_event_with_optional_calendar_block(self) -> None:
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            csrf = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(1)}).json()["csrf_token"]
            created = client.post(
                "/api/v1/admin/all-day-events",
                json={
                    "subject": "Отпуск",
                    "start_date": (today + timedelta(days=5)).isoformat(),
                    "end_date": (today + timedelta(days=8)).isoformat(),
                },
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "a" * 24},
            )
            self.assertEqual(created.status_code, 200)
            self.assertTrue(created.json()["all_day"])
            self.assertEqual(created.json()["allowed_actions"], [])
            self.assertEqual(created.json()["duration_minutes"], 4 * 24 * 60)
            with self.database._connect() as connection:
                scheduled = connection.execute(
                    "SELECT COUNT(*) FROM scheduled_jobs WHERE request_id = ? AND job_type = 'MEETING_REMINDER'",
                    (created.json()["id"],),
                ).fetchone()[0]
            self.assertEqual(scheduled, 1)
            self.assertTrue(self.database.active_intervals(
                datetime.combine(today + timedelta(days=6), clock_time(10, 0), ZoneInfo("Europe/Moscow")).astimezone(ZoneInfo("UTC")),
                datetime.combine(today + timedelta(days=6), clock_time(11, 0), ZoneInfo("Europe/Moscow")).astimezone(ZoneInfo("UTC")),
            ))
            self.assertEqual(
                client.post(
                    f"/api/v1/admin/manual-meetings/{created.json()['id']}/cancel",
                    headers={"X-CSRF-Token": csrf, "Idempotency-Key": "b" * 24},
                ).status_code,
                200,
            )
            nonblocking = client.post(
                "/api/v1/admin/all-day-events",
                json={
                    "subject": "Каникулы детей",
                    "start_date": (today + timedelta(days=10)).isoformat(),
                    "end_date": (today + timedelta(days=11)).isoformat(),
                    "blocks_calendar": False,
                },
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": "c" * 24},
            )
            self.assertEqual(nonblocking.status_code, 200)
            self.assertTrue(nonblocking.json()["all_day"])
            self.assertFalse(nonblocking.json()["blocks_calendar"])
            self.assertFalse(self.database.active_intervals(
                datetime.combine(today + timedelta(days=10), clock_time(10, 0), ZoneInfo("Europe/Moscow")).astimezone(ZoneInfo("UTC")),
                datetime.combine(today + timedelta(days=10), clock_time(11, 0), ZoneInfo("Europe/Moscow")).astimezone(ZoneInfo("UTC")),
            ))

    def test_admin_can_create_and_cancel_series(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=5), clock_time(11, 0), zone)
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            csrf = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(1)}).json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "p" * 24}
            created = client.post(
                "/api/v1/admin/series",
                json={
                    "subject": "Еженедельная встреча",
                    "start_at": start.isoformat(),
                    "duration_minutes": 30,
                    "frequency": "WEEKLY",
                    "until_date": (start.date() + timedelta(days=21)).isoformat(),
                },
                headers=headers,
            )
            self.assertEqual(created.status_code, 200)
            self.assertEqual(created.json()["status"], "ACTIVE")
            self.assertEqual(len(client.get("/api/v1/admin/series").json()["items"]), 1)
            occurrences = client.get(f"/api/v1/admin/series/{created.json()['id']}/occurrences")
            self.assertEqual(occurrences.status_code, 200)
            occurrence_id = occurrences.json()["items"][0]["id"]
            moved = client.patch(
                f"/api/v1/admin/series/{created.json()['id']}/occurrences/{occurrence_id}",
                json={"start_at": (start + timedelta(days=1)).isoformat(), "duration_minutes": 30},
                headers={**headers, "Idempotency-Key": "t" * 24},
            )
            self.assertEqual(moved.status_code, 200)
            self.assertEqual(moved.json()["status"], "MOVED")
            occurrence_cancelled = client.post(
                f"/api/v1/admin/series/{created.json()['id']}/occurrences/{occurrence_id}/cancel",
                headers={**headers, "Idempotency-Key": "u" * 24},
            )
            self.assertEqual(occurrence_cancelled.status_code, 200)
            self.assertEqual(occurrence_cancelled.json()["status"], "CANCELLED")
            cancelled = client.post(
                f"/api/v1/admin/series/{created.json()['id']}/cancel",
                headers={**headers, "Idempotency-Key": "q" * 24},
            )
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["status"], "CANCELLED")

    def test_admin_can_approve_or_reject_change_request(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=5), clock_time(11, 0), zone).astimezone(ZoneInfo("UTC"))
        request = self.database.create_request(
            telegram_id=100, telegram_name="Тест", telegram_username="tester", email="test@example.com",
            subject="Изменение", description=None, location=None, start_at=start, end_at=start + timedelta(minutes=30), hold_hours=24,
        )
        self.assertIsNotNone(self.database.claim_for_approval(request.id, 1))
        self.database.complete_approval(request.id, 1, "test-google-event")
        cancel_change = self.database.create_change_request(request.id, 100, CHANGE_CANCEL)
        second_request = self.database.create_request(
            telegram_id=100, telegram_name="Тест", telegram_username="tester", email="test@example.com",
            subject="Перенос", description=None, location=None, start_at=start + timedelta(days=1), end_at=start + timedelta(days=1, minutes=30), hold_hours=24,
        )
        self.assertIsNotNone(self.database.claim_for_approval(second_request.id, 1))
        self.database.complete_approval(second_request.id, 1, "test-google-event-second")
        reschedule_change = self.database.create_change_request(
            second_request.id, 100, "RESCHEDULE", start + timedelta(days=3), start + timedelta(days=3, minutes=30)
        )
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            csrf = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data(1)}).json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "r" * 24}
            self.assertEqual(len(client.get("/api/v1/admin/change-requests").json()["items"]), 2)
            rejected = client.post(f"/api/v1/admin/change-requests/{reschedule_change.id}/reject", headers=headers)
            self.assertEqual(rejected.status_code, 200)
            self.assertEqual(rejected.json()["status"], "REJECTED")
            approved = client.post(
                f"/api/v1/admin/change-requests/{cancel_change.id}/approve",
                headers={**headers, "Idempotency-Key": "s" * 24},
            )
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["request"]["status"], "CANCELLED_BY_ADMIN")

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

    def test_http_accepts_only_owned_alternative(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=3), clock_time(10, 0), zone).astimezone(ZoneInfo("UTC"))
        request = self.database.create_request(
            telegram_id=100,
            telegram_name="Тест",
            telegram_username="tester",
            email="test@example.com",
            subject="Альтернатива",
            description=None,
            location=None,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            hold_hours=24,
        )
        alternative = self.database.create_alternative(
            request.id,
            1,
            start + timedelta(days=1),
            start + timedelta(days=1, minutes=30),
            24,
        )
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            auth = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data()})
            csrf_token = auth.json()["csrf_token"]
            listed = client.get(f"/api/v1/requests/{request.id}/alternatives")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["items"][0]["id"], str(alternative.id))
            accepted = client.post(
                f"/api/v1/requests/{request.id}/alternatives/{alternative.id}/accept",
                headers={"Idempotency-Key": "e" * 24, "X-CSRF-Token": csrf_token},
            )
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json()["id"], str(request.id))

    def test_http_creates_change_request_and_confirms_keep_future_deletion(self) -> None:
        zone = ZoneInfo("Europe/Moscow")
        start = datetime.combine(datetime.now(zone).date() + timedelta(days=3), clock_time(10, 0), zone).astimezone(ZoneInfo("UTC"))
        request = self.database.create_request(
            telegram_id=100,
            telegram_name="Тест",
            telegram_username="tester",
            email="test@example.com",
            subject="Назначенная встреча",
            description=None,
            location=None,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            hold_hours=24,
        )
        self.assertIsNotNone(self.database.claim_for_approval(request.id, 1))
        self.database.complete_approval(request.id, 1, "test-google-event")
        app = create_app(self.settings, self.database, FreeCalendar(), cookie_secure=False)
        with TestClient(app) as client:
            auth = client.post("/api/v1/auth/telegram", json={"init_data": self._signed_init_data()})
            csrf_token = auth.json()["csrf_token"]
            changed = client.post(
                f"/api/v1/requests/{request.id}/change-requests",
                json={"change_type": "RESCHEDULE", "start_at": (start + timedelta(days=2)).isoformat(), "duration_minutes": 30},
                headers={"Idempotency-Key": "f" * 24, "X-CSRF-Token": csrf_token},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["change_type"], "RESCHEDULE")

            listed = client.get("/api/v1/requests")
            self.assertEqual(listed.status_code, 200)
            item = listed.json()["items"][0]
            self.assertEqual(item["open_change"]["change_type"], "RESCHEDULE")
            self.assertEqual(item["open_change"]["status"], "PENDING")
            self.assertEqual(item["allowed_actions"], [])

            created = client.post(
                "/api/v1/deletion-requests",
                json={"mode": DELETE_KEEP_FUTURE},
                headers={"Idempotency-Key": "g" * 24, "X-CSRF-Token": csrf_token},
            )
            self.assertEqual(created.status_code, 200)
            completed = client.post(
                f"/api/v1/deletion-requests/{created.json()['id']}/confirm",
                headers={"Idempotency-Key": "h" * 24, "X-CSRF-Token": csrf_token},
            )
            self.assertEqual(completed.status_code, 200)
            self.assertNotEqual(completed.json()["status"], DELETE_COMPLETED)


if __name__ == "__main__":
    unittest.main()

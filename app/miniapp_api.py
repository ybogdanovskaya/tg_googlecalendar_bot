from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.apps_script_calendar import AppsScriptCalendar
from app.booking_rules import BOOKING_ENABLED, BOOKING_HORIZON_DAYS, DURATIONS, HOLD_HOURS, MIN_LEAD_MINUTES, STEP_MINUTES, USER_BOOKING_WINDOW, validate_value as validate_booking_setting
from app.calendar_client import CalendarClient, CalendarUnavailable
from app.config import Settings
from app.db import Database
from app.google_calendar import GoogleCalendar
from app.miniapp_services import (
    BookingValidationError,
    ConsentRequiredError,
    IdempotencyConflictError,
    MiniAppBookingService,
    RequestNotEditableError,
    SlotConflictError,
)
from app.models import (
    APPROVED,
    APPROVING,
    CANCELLED,
    CANCELLED_BY_ADMIN,
    PENDING,
    REJECTED,
    ChangeRequest,
    EventSeries,
    EventOccurrence,
    MeetingRequest,
    RequestAlternative,
)
from app.notification_rules import AUTOMATION_ENABLED, PENDING_REMINDER_HOURS, REMINDER_MINUTES, load_notification_rules, validate_value as validate_notification_setting
from app.release_d_store import DeletionRequest, ReleaseDStore


COOKIE_NAME = "__Host-calendar_session"


class ApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, retryable: bool = False) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class Actor:
    telegram_id: int
    csrf_hash: str
    expires_at: datetime
    role: str


class TelegramAuthBody(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


class ConsentBody(BaseModel):
    accepted: bool


class RequestCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=1000)
    start_at: datetime
    duration_minutes: int


class RequestPatchBody(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    subject: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=1000)
    start_at: datetime | None = None
    duration_minutes: int | None = None


class ChangeRequestBody(BaseModel):
    change_type: str = Field(pattern="^(CANCEL|RESCHEDULE)$")
    start_at: datetime | None = None
    duration_minutes: int | None = None


class DeletionRequestBody(BaseModel):
    mode: str = Field(pattern="^(CANCEL_FUTURE|KEEP_FUTURE)$")


class AdminAlternativeBody(BaseModel):
    start_at: datetime
    duration_minutes: int = Field(ge=1, le=480)


class AdminManualMeetingBody(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    description: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=1000)
    start_at: datetime
    duration_minutes: int = Field(ge=1, le=480)
    blocks_calendar: bool = True
    allow_overlap: bool = False


class AdminRequestPatchBody(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    subject: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=1000)


class AdminSeriesBody(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    description: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=1000)
    start_at: datetime
    duration_minutes: int = Field(ge=1, le=480)
    frequency: str = Field(pattern="^(DAILY|WEEKLY|MONTHLY)$")
    until_date: date
    blocks_calendar: bool = True
    allow_overlap: bool = False


class AdminOccurrenceMoveBody(BaseModel):
    start_at: datetime
    duration_minutes: int = Field(ge=1, le=480)


class AdminSettingBody(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    value: Any


class ClosedDateBody(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


def _request_id() -> str:
    return hashlib.sha256(f"{time.time_ns()}:{os.urandom(16).hex()}".encode("utf-8")).hexdigest()[:20]


def _api_error_response(error: ApiError, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message, "retryable": error.retryable, "request_id": request_id or _request_id()}},
    )


def validate_telegram_init_data(raw: str, bot_token: str, max_age_seconds: int = 300) -> dict[str, Any]:
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ApiError(401, "AUTH_INVALID", "Не удалось подтвердить Telegram-сессию.") from exc
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise ApiError(401, "AUTH_INVALID", "Не удалось подтвердить Telegram-сессию.")
        values[key] = value
    received_hash = values.pop("hash", "")
    if not received_hash or "auth_date" not in values or "user" not in values:
        raise ApiError(401, "AUTH_INVALID", "Не удалось подтвердить Telegram-сессию.")
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise ApiError(401, "AUTH_INVALID", "Не удалось подтвердить Telegram-сессию.")
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        telegram_id = int(user["id"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ApiError(401, "AUTH_INVALID", "Не удалось подтвердить Telegram-сессию.") from exc
    now = int(time.time())
    if auth_date > now + 30 or now - auth_date > max_age_seconds or telegram_id <= 0:
        raise ApiError(401, "AUTH_INVALID", "Telegram-сессия устарела. Откройте Mini App заново.")
    if not isinstance(user, dict):
        raise ApiError(401, "AUTH_INVALID", "Не удалось подтвердить Telegram-сессию.")
    return user


def request_view(value: MeetingRequest, timezone_name: str) -> dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    labels = {
        PENDING: "На согласовании",
        APPROVING: "Создаётся событие",
        APPROVED: "Назначена",
        REJECTED: "Отклонена",
        CANCELLED: "Отменена пользователем",
        CANCELLED_BY_ADMIN: "Отменена администратором",
    }
    now = datetime.now(UTC)
    actions: list[str] = []
    if value.status == PENDING:
        actions = ["EDIT", "CANCEL"]
    elif value.status == APPROVED and value.end_at > now:
        actions = ["REQUEST_RESCHEDULE", "REQUEST_CANCEL"]
    return {
        "id": str(value.id), "subject": value.subject, "description": value.description, "location": value.location,
        "email": value.email, "name": value.telegram_name,
        "start_at": value.start_at.astimezone(zone).isoformat(), "end_at": value.end_at.astimezone(zone).isoformat(),
        "duration_minutes": int((value.end_at - value.start_at).total_seconds() // 60),
        "status": value.status, "status_label": labels.get(value.status, "Обновляется"),
        "reservation": {"active": value.status == PENDING and value.hold_until > now, "until": value.hold_until.astimezone(zone).isoformat()},
        "allowed_actions": actions, "created_at": value.created_at.astimezone(zone).isoformat(), "updated_at": value.updated_at.astimezone(zone).isoformat(),
    }


def alternative_view(value: RequestAlternative, timezone_name: str) -> dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    return {
        "id": str(value.id),
        "start_at": value.start_at.astimezone(zone).isoformat(),
        "end_at": value.end_at.astimezone(zone).isoformat(),
        "duration_minutes": int((value.end_at - value.start_at).total_seconds() // 60),
        "expires_at": value.hold_until.astimezone(zone).isoformat(),
    }


def series_view(value: EventSeries, timezone_name: str) -> dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    return {
        "id": str(value.id),
        "subject": value.subject,
        "email": value.email,
        "description": value.description,
        "location": value.location,
        "start_at": value.start_at.astimezone(zone).isoformat(),
        "end_at": value.end_at.astimezone(zone).isoformat(),
        "frequency": value.frequency,
        "until_date": value.until_date,
        "status": value.status,
        "blocks_calendar": value.blocks_calendar,
        "allow_overlap": value.allow_overlap,
    }


def occurrence_view(value: EventOccurrence, timezone_name: str) -> dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    return {
        "id": str(value.id), "series_id": str(value.series_id), "status": value.status,
        "start_at": value.actual_start_at.astimezone(zone).isoformat(),
        "end_at": value.actual_end_at.astimezone(zone).isoformat(),
    }


def change_view(value: ChangeRequest, timezone_name: str) -> dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    return {
        "id": str(value.id),
        "change_type": value.change_type,
        "status": value.status,
        "proposed_start_at": value.proposed_start_at.astimezone(zone).isoformat() if value.proposed_start_at else None,
        "proposed_end_at": value.proposed_end_at.astimezone(zone).isoformat() if value.proposed_end_at else None,
        "created_at": value.created_at.astimezone(zone).isoformat(),
    }


def deletion_view(value: DeletionRequest, timezone_name: str) -> dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    return {
        "id": str(value.id),
        "mode": value.mode,
        "status": value.status,
        "future_meeting_count": value.future_meeting_count,
        "execute_after": value.execute_after.astimezone(zone).isoformat() if value.execute_after else None,
    }


def create_app(
    settings: Settings,
    database: Database,
    calendar: CalendarClient,
    *,
    cookie_secure: bool = True,
    initdata_max_age_seconds: int = 300,
    session_ttl_seconds: int = 1800,
) -> FastAPI:
    app = FastAPI(title="Telegram Calendar Mini App API", docs_url=None, redoc_url=None, openapi_url=None)
    service = MiniAppBookingService(settings, database, calendar)
    session_cookie_name = COOKIE_NAME if cookie_secure else "calendar_session"

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
        return _api_error_response(error, getattr(request.state, "request_id", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, _: RequestValidationError) -> JSONResponse:
        return _api_error_response(ApiError(422, "VALIDATION_ERROR", "Проверьте заполнение полей."), getattr(request.state, "request_id", None))

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = _request_id()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    async def actor(request: Request) -> Actor:
        session_token = request.cookies.get(session_cookie_name)
        if not session_token:
            raise ApiError(401, "AUTH_REQUIRED", "Откройте Mini App через Telegram.")
        session = await asyncio.to_thread(database.get_miniapp_session, session_token)
        if session is None:
            raise ApiError(401, "AUTH_REQUIRED", "Сессия истекла. Откройте Mini App заново.")
        role = "ADMIN" if session.telegram_id == settings.admin_telegram_id else "USER"
        return Actor(session.telegram_id, session.csrf_hash, session.expires_at, role)

    async def mutation_actor(
        request: Request,
        current: Actor = Depends(actor),
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
        origin: str | None = Header(default=None, alias="Origin"),
    ) -> Actor:
        if not csrf_token or not hmac.compare_digest(current.csrf_hash, hashlib.sha256(csrf_token.encode("utf-8")).hexdigest()):
            raise ApiError(403, "CSRF_INVALID", "Не удалось подтвердить действие.")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise ApiError(403, "CSRF_INVALID", "Не удалось подтвердить действие.")
        return current

    async def admin_actor(current: Actor = Depends(mutation_actor)) -> Actor:
        if current.role != "ADMIN":
            raise ApiError(403, "ACCESS_DENIED", "Нет доступа к управлению.")
        return current

    async def admin_reader(current: Actor = Depends(actor)) -> Actor:
        if current.role != "ADMIN":
            raise ApiError(403, "ACCESS_DENIED", "Нет доступа к управлению.")
        return current

    def idempotency_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
        if not value or len(value) > 128:
            raise ApiError(422, "VALIDATION_ERROR", "Для действия требуется ключ повтора.")
        return value

    @app.post("/api/v1/auth/telegram")
    async def telegram_auth(body: TelegramAuthBody, response: Response) -> dict[str, Any]:
        user = validate_telegram_init_data(body.init_data, settings.bot_token, initdata_max_age_seconds)
        telegram_id = int(user["id"])
        name = " ".join(item for item in (str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()) if item) or "Пользователь"
        username_raw = user.get("username")
        username = str(username_raw) if isinstance(username_raw, str) and username_raw else None
        await asyncio.to_thread(database.upsert_user, telegram_id, name, username)
        token, csrf_token, expires_at = await asyncio.to_thread(database.create_miniapp_session, telegram_id, session_ttl_seconds)
        response.set_cookie(session_cookie_name, token, max_age=session_ttl_seconds, path="/", secure=cookie_secure, httponly=True, samesite="lax")
        return {
            "user": {"display_name": name, "role": "ADMIN" if telegram_id == settings.admin_telegram_id else "USER", "consent": {"accepted": await asyncio.to_thread(database.has_consent, telegram_id, settings.privacy_policy_version), "version": settings.privacy_policy_version}},
            "csrf_token": csrf_token, "expires_at": expires_at.isoformat(),
        }

    @app.delete("/api/v1/auth/session")
    async def logout(request: Request, response: Response) -> Response:
        session_token = request.cookies.get(session_cookie_name)
        if session_token:
            await asyncio.to_thread(database.delete_miniapp_session, session_token)
        response.delete_cookie(session_cookie_name, path="/")
        response.status_code = 204
        return response

    @app.get("/api/v1/me")
    async def get_me(current: Actor = Depends(actor)) -> dict[str, Any]:
        consent = await asyncio.to_thread(database.has_consent, current.telegram_id, settings.privacy_policy_version)
        return {"role": current.role, "consent": {"accepted": consent, "version": settings.privacy_policy_version}, "timezone": settings.timezone, "expires_at": current.expires_at.isoformat()}

    @app.get("/api/v1/admin/dashboard")
    async def admin_dashboard(_: Actor = Depends(admin_reader)) -> dict[str, Any]:
        now = datetime.now(UTC)
        statistics = await asyncio.to_thread(ReleaseDStore(database).statistics, now - timedelta(days=30), now)
        pending = await asyncio.to_thread(database.list_pending, 100)
        changes = await asyncio.to_thread(database.list_pending_changes, 100)
        return {
            "pending_requests": len(pending),
            "pending_changes": len(changes),
            "statistics": {
                "user_requests": statistics.user_requests,
                "manual_meetings": statistics.manual_meetings,
                "calendar_meetings": statistics.calendar_meetings,
                "unique_users": statistics.unique_users,
            },
        }

    @app.get("/api/v1/admin/statistics")
    async def admin_statistics(
        from_date: date | None = None,
        to_date: date | None = None,
        _: Actor = Depends(admin_reader),
    ) -> dict[str, Any]:
        zone = ZoneInfo(settings.timezone)
        today = datetime.now(zone).date()
        period_from = from_date or today - timedelta(days=29)
        period_to = to_date or today
        if period_to < period_from or period_to > period_from + timedelta(days=366):
            raise ApiError(422, "VALIDATION_ERROR", "Период статистики должен составлять от одного дня до года.")
        start = datetime.combine(period_from, datetime.min.time(), zone).astimezone(UTC)
        end = datetime.combine(period_to + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
        value = await asyncio.to_thread(ReleaseDStore(database).statistics, start, end)
        return {
            "from_date": period_from.isoformat(), "to_date": period_to.isoformat(),
            "user_requests": value.user_requests, "manual_meetings": value.manual_meetings,
            "calendar_meetings": value.calendar_meetings, "unique_users": value.unique_users,
        }

    @app.get("/api/v1/admin/integration/calendar")
    async def admin_calendar_integration(_: Actor = Depends(admin_reader)) -> dict[str, Any]:
        checked_at = datetime.now(UTC)
        try:
            await calendar.is_free(checked_at, checked_at + timedelta(minutes=1))
        except Exception:
            return {"status": "UNAVAILABLE", "checked_at": checked_at.astimezone(ZoneInfo(settings.timezone)).isoformat()}
        return {"status": "OK", "checked_at": checked_at.astimezone(ZoneInfo(settings.timezone)).isoformat()}

    @app.get("/api/v1/admin/requests")
    async def admin_requests(_: Actor = Depends(admin_reader)) -> dict[str, Any]:
        items = await asyncio.to_thread(database.list_pending, 100)
        return {"items": [request_view(item, settings.timezone) for item in items]}

    @app.get("/api/v1/admin/requests/{request_id}")
    async def admin_request(request_id: int, _: Actor = Depends(admin_reader)) -> dict[str, Any]:
        value = await asyncio.to_thread(database.get_request, request_id)
        if value is None:
            raise ApiError(404, "NOT_FOUND", "Заявка не найдена.")
        return request_view(value, settings.timezone)

    @app.patch("/api/v1/admin/requests/{request_id}")
    async def admin_update_request(request_id: int, body: AdminRequestPatchBody, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        fields = {"name": "telegram_name", "email": "email", "subject": "subject", "description": "description", "location": "location"}
        changes = {target: getattr(body, source) for source, target in fields.items() if source in body.model_fields_set}
        if not changes:
            raise ApiError(422, "VALIDATION_ERROR", "Укажите хотя бы одно поле для изменения.")
        try:
            value = await asyncio.to_thread(database.admin_update_pending_details, request_id, current.telegram_id, changes)
        except (RequestNotEditableError, ValueError) as exc:
            raise ApiError(409, "CONFLICT", "Заявка больше недоступна для редактирования.") from exc
        return request_view(value, settings.timezone)

    @app.get("/api/v1/admin/change-requests")
    async def admin_change_requests(_: Actor = Depends(admin_reader)) -> dict[str, Any]:
        changes = await asyncio.to_thread(database.list_pending_changes, 100)
        items: list[dict[str, Any]] = []
        for change in changes:
            request = await asyncio.to_thread(database.get_request, change.request_id)
            if request is not None:
                items.append({"change": change_view(change, settings.timezone), "request": request_view(request, settings.timezone)})
        return {"items": items}

    @app.post("/api/v1/admin/change-requests/{change_id}/approve")
    async def admin_approve_change(change_id: int, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            change, request = await service.admin_approve_change(change_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Запрос на изменение уже недоступен.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "Новое время уже занято. Запрос возвращён на согласование.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return {"change": change_view(change, settings.timezone), "request": request_view(request, settings.timezone)}

    @app.post("/api/v1/admin/change-requests/{change_id}/reject")
    async def admin_reject_change(change_id: int, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_reject_change(change_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Запрос на изменение уже недоступен.") from exc
        return change_view(value, settings.timezone)

    @app.post("/api/v1/admin/requests/{request_id}/approve")
    async def admin_approve_request(request_id: int, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_approve_request(request_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Заявка уже обработана.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "Время уже занято. Заявка возвращена на согласование.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен. Заявка сохранена.", True) from exc
        return request_view(value, settings.timezone)

    @app.post("/api/v1/admin/requests/{request_id}/reject")
    async def admin_reject_request(request_id: int, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_reject_request(request_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Заявка уже обработана.") from exc
        return request_view(value, settings.timezone)

    @app.post("/api/v1/admin/requests/{request_id}/alternatives")
    async def admin_create_alternative(request_id: int, body: AdminAlternativeBody, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_create_alternative(request_id, current.telegram_id, body.start_at, body.duration_minutes)
        except BookingValidationError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Укажите корректное время альтернативы.") from exc
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Для этой заявки нельзя предложить другое время.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "Это время уже занято. Выберите другой слот.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return alternative_view(value, settings.timezone)

    @app.post("/api/v1/admin/manual-meetings")
    async def admin_create_manual_meeting(body: AdminManualMeetingBody, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_create_manual_meeting(
                admin_id=current.telegram_id,
                subject=body.subject,
                email=body.email,
                description=body.description,
                location=body.location,
                start_at=body.start_at,
                duration_minutes=body.duration_minutes,
                blocks_calendar=body.blocks_calendar,
                allow_overlap=body.allow_overlap,
            )
        except BookingValidationError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Данные встречи некорректны.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "В это время уже есть встреча.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return request_view(value, settings.timezone)

    @app.get("/api/v1/admin/manual-meetings")
    async def admin_manual_meetings(current: Actor = Depends(admin_reader)) -> dict[str, Any]:
        items = await asyncio.to_thread(database.list_admin_meetings, current.telegram_id, 50)
        return {"items": [request_view(item, settings.timezone) for item in items]}

    @app.post("/api/v1/admin/manual-meetings/{request_id}/cancel")
    async def admin_cancel_manual_meeting(request_id: int, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_cancel_manual_meeting(request_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Ручная встреча больше недоступна.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Не удалось отменить встречу в календаре.", True) from exc
        return request_view(value, settings.timezone)

    @app.patch("/api/v1/admin/manual-meetings/{request_id}")
    async def admin_update_manual_meeting(request_id: int, body: AdminRequestPatchBody, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        fields = {"name": "telegram_name", "email": "email", "subject": "subject", "description": "description", "location": "location"}
        changes = {target: getattr(body, source) for source, target in fields.items() if source in body.model_fields_set}
        if not changes:
            raise ApiError(422, "VALIDATION_ERROR", "Укажите хотя бы одно поле для изменения.")
        try:
            value = await service.admin_update_manual_meeting(request_id, current.telegram_id, changes)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Ручная встреча больше недоступна для редактирования.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Не удалось обновить встречу в календаре.", True) from exc
        return request_view(value, settings.timezone)

    @app.get("/api/v1/admin/series")
    async def admin_series(current: Actor = Depends(admin_reader)) -> dict[str, Any]:
        items = await asyncio.to_thread(service.automation.list_series, current.telegram_id, 50)
        return {"items": [series_view(item, settings.timezone) for item in items]}

    @app.post("/api/v1/admin/series")
    async def admin_create_series(body: AdminSeriesBody, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_create_series(
                admin_id=current.telegram_id,
                subject=body.subject,
                email=body.email,
                description=body.description,
                location=body.location,
                start_at=body.start_at,
                duration_minutes=body.duration_minutes,
                frequency=body.frequency,
                until_date=body.until_date,
                blocks_calendar=body.blocks_calendar,
                allow_overlap=body.allow_overlap,
            )
        except BookingValidationError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Параметры серии некорректны.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "Одна из встреч серии пересекается с занятым временем.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return series_view(value, settings.timezone)

    @app.post("/api/v1/admin/series/{series_id}/cancel")
    async def admin_cancel_series(series_id: int, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_cancel_series(series_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Серия больше недоступна.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Не удалось отменить серию в календаре.", True) from exc
        return series_view(value, settings.timezone)

    @app.get("/api/v1/admin/series/{series_id}/occurrences")
    async def admin_series_occurrences(series_id: int, current: Actor = Depends(admin_reader)) -> dict[str, Any]:
        series = await asyncio.to_thread(service.automation.get_series, series_id)
        if series is None or series.created_by != current.telegram_id:
            raise ApiError(404, "NOT_FOUND", "Серия не найдена.")
        items = await asyncio.to_thread(service.automation.list_occurrences, series_id, future_only=True, limit=100)
        return {"items": [occurrence_view(item, settings.timezone) for item in items]}

    @app.post("/api/v1/admin/series/{series_id}/occurrences/{occurrence_id}/cancel")
    async def admin_cancel_occurrence(series_id: int, occurrence_id: int, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_cancel_occurrence(series_id, occurrence_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Повторение больше недоступно.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Не удалось отменить повторение в календаре.", True) from exc
        return occurrence_view(value, settings.timezone)

    @app.patch("/api/v1/admin/series/{series_id}/occurrences/{occurrence_id}")
    async def admin_move_occurrence(series_id: int, occurrence_id: int, body: AdminOccurrenceMoveBody, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.admin_move_occurrence(series_id, occurrence_id, current.telegram_id, body.start_at, body.duration_minutes)
        except BookingValidationError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Новое время повторения некорректно.") from exc
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Повторение больше недоступно.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Не удалось перенести повторение в календаре.", True) from exc
        return occurrence_view(value, settings.timezone)

    @app.get("/api/v1/admin/settings")
    async def admin_settings(_: Actor = Depends(admin_reader)) -> dict[str, Any]:
        rules = await asyncio.to_thread(service.rules)
        notifications = await asyncio.to_thread(load_notification_rules, database, settings)
        return {
            "booking": {"booking_enabled": rules.booking_enabled, "min_lead_minutes": rules.min_lead_minutes, "booking_horizon_days": rules.booking_horizon_days, "hold_hours": rules.hold_hours, "durations": list(rules.durations), "step_minutes": rules.step_minutes, "user_booking_window": [rules.user_booking_start_minutes, rules.user_booking_end_minutes]},
            "notifications": {"reminder_minutes": list(notifications.reminder_minutes), "pending_reminder_hours": notifications.pending_reminder_hours, "automation_enabled": notifications.automation_enabled},
        }

    @app.patch("/api/v1/admin/settings")
    async def admin_update_setting(body: AdminSettingBody, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        booking_keys = {BOOKING_ENABLED, MIN_LEAD_MINUTES, BOOKING_HORIZON_DAYS, HOLD_HOURS, DURATIONS, STEP_MINUTES, USER_BOOKING_WINDOW}
        notification_keys = {REMINDER_MINUTES, PENDING_REMINDER_HOURS, AUTOMATION_ENABLED}
        try:
            value = validate_booking_setting(body.key, body.value) if body.key in booking_keys else validate_notification_setting(body.key, body.value) if body.key in notification_keys else None
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Недопустимое значение настройки.") from exc
        if value is None:
            raise ApiError(422, "VALIDATION_ERROR", "Неизвестная настройка.")
        await asyncio.to_thread(database.set_setting, body.key, value, current.telegram_id)
        return {"key": body.key, "value": value}

    @app.get("/api/v1/admin/closed-dates")
    async def admin_closed_dates(_: Actor = Depends(admin_reader)) -> dict[str, Any]:
        return {"items": await asyncio.to_thread(database.list_closed_dates, None, 365)}

    @app.post("/api/v1/admin/closed-dates")
    async def admin_add_closed_date(body: ClosedDateBody, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            datetime.fromisoformat(body.date)
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Укажите корректную дату.") from exc
        added = await asyncio.to_thread(database.add_closed_date, body.date, current.telegram_id)
        return {"date": body.date, "added": added}

    @app.delete("/api/v1/admin/closed-dates/{local_date}")
    async def admin_remove_closed_date(local_date: str, current: Actor = Depends(admin_actor), _: str = Depends(idempotency_key)) -> Response:
        removed = await asyncio.to_thread(database.remove_closed_date, local_date, current.telegram_id)
        if not removed:
            raise ApiError(404, "NOT_FOUND", "Закрытая дата не найдена.")
        return Response(status_code=204)

    @app.post("/api/v1/consents")
    async def accept_consent(body: ConsentBody, current: Actor = Depends(mutation_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        if not body.accepted:
            raise ApiError(422, "VALIDATION_ERROR", "Необходимо явное согласие.")
        await asyncio.to_thread(database.set_consent, current.telegram_id, settings.privacy_policy_version)
        return {"accepted": True, "version": settings.privacy_policy_version}

    @app.get("/api/v1/privacy-policy")
    async def privacy_policy(_: Actor = Depends(actor)) -> dict[str, Any]:
        return {
            "version": settings.privacy_policy_version,
            "summary": "Контактные данные используются только для обработки заявки, уведомлений и организации встречи.",
            "calendar_privacy": "Содержимое личного Google Calendar не передаётся Mini App.",
            "rights": ["Можно запросить удаление данных и отмену будущих встреч.", "Можно сохранить будущие встречи с минимальными данными до их завершения."],
        }

    @app.get("/api/v1/booking/config")
    async def booking_config(_: Actor = Depends(actor)) -> dict[str, Any]:
        rules = await asyncio.to_thread(service.rules)
        return {"timezone": settings.timezone, "booking_enabled": rules.booking_enabled, "durations": list(rules.durations), "step_minutes": rules.step_minutes, "horizon_days": rules.booking_horizon_days, "min_lead_minutes": rules.min_lead_minutes, "window": {"start_minutes": rules.user_booking_start_minutes, "end_minutes": rules.user_booking_end_minutes}}

    @app.get("/api/v1/booking/calendar")
    async def booking_calendar(from_date: str, to_date: str, _: Actor = Depends(actor)) -> dict[str, Any]:
        try:
            start, end = datetime.fromisoformat(from_date).date(), datetime.fromisoformat(to_date).date()
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Укажите корректный период календаря.") from exc
        if end < start or (end - start).days > 62:
            raise ApiError(422, "VALIDATION_ERROR", "Период календаря недопустим.")
        return await asyncio.to_thread(service.calendar_dates, start, end)

    @app.get("/api/v1/booking/slots")
    async def booking_slots(date: str, duration_minutes: int, _: Actor = Depends(actor)) -> dict[str, Any]:
        try:
            result = await service.slots(datetime.fromisoformat(date).date(), duration_minutes)
        except BookingValidationError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Этот день или длительность недоступны.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return {"slots": [item.isoformat() for item in result.slots], "period_counts": result.period_counts}

    @app.get("/api/v1/requests")
    async def list_requests(current: Actor = Depends(actor)) -> dict[str, Any]:
        items = await asyncio.to_thread(database.list_user_requests, current.telegram_id, 100)
        return {"items": [request_view(item, settings.timezone) for item in items]}

    @app.get("/api/v1/requests/{request_id}")
    async def get_request(request_id: int, current: Actor = Depends(actor)) -> dict[str, Any]:
        value = await asyncio.to_thread(database.get_request, request_id)
        if value is None or value.telegram_id != current.telegram_id:
            raise ApiError(404, "NOT_FOUND", "Заявка не найдена.")
        return request_view(value, settings.timezone)

    @app.post("/api/v1/requests")
    async def create_request(body: RequestCreateBody, current: Actor = Depends(mutation_actor), key: str = Depends(idempotency_key)) -> JSONResponse:
        try:
            value, replayed = await service.create_request(
                telegram_id=current.telegram_id, telegram_name=body.name, telegram_username=None, name=body.name, email=body.email,
                subject=body.subject, description=body.description, location=body.location, start_at=body.start_at,
                duration_minutes=body.duration_minutes, idempotency_key=key,
            )
        except ConsentRequiredError as exc:
            raise ApiError(403, "CONSENT_REQUIRED", "Сначала примите политику конфиденциальности.") from exc
        except BookingValidationError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Этот слот недоступен.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "Это время уже занято. Выберите другой слот.") from exc
        except IdempotencyConflictError as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Ключ действия уже использован для других данных.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return JSONResponse(status_code=200 if replayed else 201, content=request_view(value, settings.timezone))

    @app.patch("/api/v1/requests/{request_id}")
    async def patch_request(request_id: int, body: RequestPatchBody, current: Actor = Depends(mutation_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        raw = body.model_dump(exclude_unset=True) if hasattr(body, "model_dump") else body.dict(exclude_unset=True)
        changes = {key: raw[key] for key in ("name", "email", "subject", "description", "location") if key in raw}
        if "name" in changes:
            changes["telegram_name"] = changes.pop("name")
        if not changes and "start_at" not in raw and "duration_minutes" not in raw:
            raise ApiError(422, "VALIDATION_ERROR", "Не переданы изменения.")
        try:
            value = await service.update_request(request_id=request_id, telegram_id=current.telegram_id, changes=changes, start_at=raw.get("start_at"), duration_minutes=raw.get("duration_minutes"))
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Заявку больше нельзя изменить.") from exc
        except BookingValidationError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Этот слот недоступен.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "Это время уже занято. Выберите другой слот.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return request_view(value, settings.timezone)

    @app.post("/api/v1/requests/{request_id}/cancel")
    async def cancel_request(request_id: int, current: Actor = Depends(mutation_actor), _: str = Depends(idempotency_key)) -> Response:
        changed = await service.cancel_pending_request(request_id, current.telegram_id)
        if not changed:
            raise ApiError(409, "CONFLICT", "Заявку больше нельзя отменить.")
        return Response(status_code=204)

    @app.get("/api/v1/requests/{request_id}/alternatives")
    async def list_alternatives(request_id: int, current: Actor = Depends(actor)) -> dict[str, Any]:
        try:
            alternatives = await service.alternatives(request_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(404, "NOT_FOUND", "Заявка не найдена.") from exc
        return {"items": [alternative_view(item, settings.timezone) for item in alternatives]}

    @app.post("/api/v1/requests/{request_id}/alternatives/{alternative_id}/accept")
    async def accept_alternative(request_id: int, alternative_id: int, current: Actor = Depends(mutation_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.accept_alternative(request_id, alternative_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Этот вариант больше недоступен.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "Этот вариант уже занят. Выберите другой.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return request_view(value, settings.timezone)

    @app.post("/api/v1/requests/{request_id}/alternatives/decline")
    async def decline_alternatives(request_id: int, current: Actor = Depends(mutation_actor), _: str = Depends(idempotency_key)) -> Response:
        try:
            await service.decline_alternatives(request_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Варианты больше недоступны.") from exc
        return Response(status_code=204)

    @app.post("/api/v1/requests/{request_id}/change-requests")
    async def create_change_request(request_id: int, body: ChangeRequestBody, current: Actor = Depends(mutation_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.create_change_request(
                request_id=request_id,
                telegram_id=current.telegram_id,
                change_type=body.change_type,
                start_at=body.start_at,
                duration_minutes=body.duration_minutes,
            )
        except BookingValidationError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Проверьте выбранное время для изменения встречи.") from exc
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Запрос на изменение больше недоступен.") from exc
        except SlotConflictError as exc:
            raise ApiError(409, "SLOT_UNAVAILABLE", "Это время уже занято. Выберите другой слот.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен.", True) from exc
        return change_view(value, settings.timezone)

    @app.post("/api/v1/deletion-requests")
    async def create_deletion_request(body: DeletionRequestBody, current: Actor = Depends(mutation_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        value = await service.create_deletion_request(current.telegram_id, body.mode)
        return deletion_view(value, settings.timezone)

    @app.post("/api/v1/deletion-requests/{deletion_id}/confirm")
    async def confirm_deletion_request(deletion_id: int, current: Actor = Depends(mutation_actor), _: str = Depends(idempotency_key)) -> dict[str, Any]:
        try:
            value = await service.confirm_deletion_request(deletion_id, current.telegram_id)
        except RequestNotEditableError as exc:
            raise ApiError(409, "CONFLICT", "Запрос на удаление больше недоступен.") from exc
        except CalendarUnavailable as exc:
            raise ApiError(503, "EXTERNAL_UNAVAILABLE", "Календарь временно недоступен. Повторите позднее.", True) from exc
        return deletion_view(value, settings.timezone)

    return app


def load_local_env(path: Path = Path(".env")) -> None:
    """Loads an ignored local .env without overriding real environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name and name.replace("_", "").isalnum():
            os.environ.setdefault(name, value)


def main() -> None:
    load_local_env()
    settings = Settings.from_env()
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    calendar: CalendarClient = AppsScriptCalendar(settings.apps_script_url, settings.apps_script_secret_file) if settings.apps_script_url else GoogleCalendar(settings.google_token_file, settings.google_calendar_id)
    import uvicorn
    uvicorn.run(create_app(settings, database, calendar, cookie_secure=os.getenv("MINIAPP_COOKIE_SECURE", "true").lower() == "true"), host=os.getenv("MINIAPP_API_BIND_HOST", "127.0.0.1"), port=int(os.getenv("MINIAPP_API_BIND_PORT", "8001")))


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.booking_rules import BookingRules, load_rules
from app.automation_store import AutomationStore
from app.calendar_client import CalendarClient, CalendarUnavailable
from app.config import Settings
from app.db import Database, IdempotencyConflictError, RequestNotEditableError, SlotConflictError
from app.models import CHANGE_CANCEL, CHANGE_RESCHEDULE, ChangeRequest, MeetingRequest, RequestAlternative
from app.notification_rules import load_notification_rules
from app.release_d_store import (
    DELETE_CANCEL_FUTURE,
    DELETE_COMPLETED,
    DELETE_KEEP_FUTURE,
    DELETE_REQUESTED,
    DELETE_WAITING,
    DeletionRequest,
    ReleaseDStore,
)
from app.slots import available_slots, booking_periods, slot_in_period


LOGGER = logging.getLogger(__name__)


class ConsentRequiredError(RuntimeError):
    pass


class BookingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SlotResult:
    slots: tuple[datetime, ...]
    period_counts: dict[str, int]


class MiniAppBookingService:
    """Shared booking use cases for the Mini App; the bot keeps its existing UI unchanged."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        calendar: CalendarClient,
        automation: AutomationStore | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.calendar = calendar
        self.automation = automation or AutomationStore(database)
        self.deletions = ReleaseDStore(database)
        self.zone = ZoneInfo(settings.timezone)

    def rules(self) -> BookingRules:
        return load_rules(self.database, self.settings)

    def ensure_consent(self, telegram_id: int) -> None:
        if not self.database.has_consent(telegram_id, self.settings.privacy_policy_version):
            raise ConsentRequiredError("privacy policy consent is required")

    def calendar_dates(self, from_date: date, to_date: date) -> dict[str, list[str]]:
        rules = self.rules()
        today = datetime.now(self.zone).date()
        allowed_from = max(from_date, today)
        allowed_to = min(to_date, today + timedelta(days=rules.booking_horizon_days - 1))
        if allowed_to < allowed_from:
            return {"available_dates": [], "closed_dates": []}
        closed = set(self.database.list_closed_dates(allowed_from.isoformat(), rules.booking_horizon_days))
        dates = [
            (allowed_from + timedelta(days=offset)).isoformat()
            for offset in range((allowed_to - allowed_from).days + 1)
        ]
        return {"available_dates": [item for item in dates if item not in closed], "closed_dates": [item for item in dates if item in closed]}

    async def slots(self, selected_date: date, duration_minutes: int) -> SlotResult:
        rules = self.rules()
        self._validate_date_and_duration(selected_date, duration_minutes, rules)
        if selected_date.isoformat() in set(self.database.list_closed_dates(selected_date.isoformat(), 1)):
            return SlotResult((), {})
        day_start = datetime.combine(selected_date, time.min, self.zone)
        day_end = day_start + timedelta(days=1)
        google_busy, local_busy = await asyncio.gather(
            self.calendar.busy(day_start, day_end),
            asyncio.to_thread(self.database.active_intervals, day_start, day_end),
        )
        values = tuple(
            available_slots(
                local_date=selected_date,
                duration_minutes=duration_minutes,
                busy_intervals=google_busy + local_busy,
                now=datetime.now(UTC),
                timezone_name=self.settings.timezone,
                min_lead_minutes=rules.min_lead_minutes,
                step_minutes=rules.step_minutes,
                window_start_minutes=rules.user_booking_start_minutes,
                window_end_minutes=rules.user_booking_end_minutes,
            )
        )
        counts = {
            period.key: sum(slot_in_period(slot, period) for slot in values)
            for period in booking_periods(rules.user_booking_start_minutes, rules.user_booking_end_minutes)
        }
        return SlotResult(values, counts)

    async def create_request(
        self,
        *,
        telegram_id: int,
        telegram_name: str,
        telegram_username: str | None,
        name: str,
        email: str,
        subject: str,
        description: str | None,
        location: str | None,
        start_at: datetime,
        duration_minutes: int,
        idempotency_key: str,
    ) -> tuple[MeetingRequest, bool]:
        self.ensure_consent(telegram_id)
        rules = self.rules()
        self._validate_request_fields(name, email, subject, description, location)
        start_utc, end_utc = self._validate_slot(start_at, duration_minutes, rules)
        if not await self.calendar.is_free(start_utc, end_utc):
            raise SlotConflictError("google calendar slot is busy")
        payload = {
            "name": name.strip(), "email": email.strip(), "subject": subject.strip(),
            "description": self._optional(description), "location": self._optional(location),
            "start_at": start_utc.isoformat(), "duration_minutes": duration_minutes,
        }
        request_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        request, replayed = await asyncio.to_thread(
            self.database.create_request_idempotent,
            telegram_id=telegram_id,
            telegram_name=name.strip() or telegram_name,
            telegram_username=telegram_username,
            email=email.strip(),
            subject=subject.strip(),
            description=self._optional(description),
            location=self._optional(location),
            start_at=start_utc,
            end_at=end_utc,
            hold_hours=rules.hold_hours,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if not replayed:
            try:
                notification_rules = await asyncio.to_thread(load_notification_rules, self.database, self.settings)
                await asyncio.to_thread(
                    self.automation.ensure_pending_reminder,
                    request.id,
                    self.settings.admin_telegram_id,
                    notification_rules.pending_reminder_hours,
                )
            except Exception:
                LOGGER.exception("miniapp_pending_reminder_schedule_failed", extra={"request_id": request.id})
        return request, replayed

    async def update_request(
        self,
        *,
        request_id: int,
        telegram_id: int,
        changes: dict[str, str | None],
        start_at: datetime | None = None,
        duration_minutes: int | None = None,
    ) -> MeetingRequest:
        self.ensure_consent(telegram_id)
        request = await asyncio.to_thread(self.database.get_request, request_id)
        if request is None or request.telegram_id != telegram_id:
            raise RequestNotEditableError("request is unavailable")
        updated = request
        if changes:
            self._validate_request_fields(
                str(changes.get("telegram_name") or request.telegram_name),
                str(changes.get("email") or request.email),
                str(changes.get("subject") or request.subject),
                changes.get("description", request.description),
                changes.get("location", request.location),
            )
            updated = await asyncio.to_thread(self.database.update_pending_details, request_id, telegram_id, changes)
        if start_at is not None or duration_minutes is not None:
            duration = duration_minutes if duration_minutes is not None else int((updated.end_at - updated.start_at).total_seconds() // 60)
            selected_start = start_at if start_at is not None else updated.start_at
            start_utc, end_utc = self._validate_slot(selected_start, duration, self.rules())
            if not await self.calendar.is_free(start_utc, end_utc):
                raise SlotConflictError("google calendar slot is busy")
            updated = await asyncio.to_thread(
                self.database.reschedule_pending, request_id, telegram_id, start_utc, end_utc, self.rules().hold_hours
            )
        return updated

    async def cancel_pending_request(self, request_id: int, telegram_id: int) -> bool:
        changed = await asyncio.to_thread(self.database.cancel_pending, request_id, telegram_id)
        if not changed:
            return False
        try:
            await asyncio.to_thread(self.automation.cancel_request_jobs, request_id)
        except Exception:
            LOGGER.exception("miniapp_cancelled_request_jobs_cleanup_failed", extra={"request_id": request_id})
        return True

    async def alternatives(self, request_id: int, telegram_id: int) -> list[RequestAlternative]:
        request = await asyncio.to_thread(self.database.get_request, request_id)
        if request is None or request.telegram_id != telegram_id:
            raise RequestNotEditableError("request is unavailable")
        return await asyncio.to_thread(self.database.list_offered_alternatives, request_id)

    async def accept_alternative(self, request_id: int, alternative_id: int, telegram_id: int) -> MeetingRequest:
        alternative = await asyncio.to_thread(self.database.get_alternative, alternative_id)
        if alternative is None or alternative.request_id != request_id:
            raise RequestNotEditableError("alternative is unavailable")
        if not await self.calendar.is_free(alternative.start_at, alternative.end_at):
            raise SlotConflictError("alternative slot is busy")
        return await asyncio.to_thread(
            self.database.accept_alternative,
            alternative_id,
            telegram_id,
            self.rules().hold_hours,
        )

    async def decline_alternatives(self, request_id: int, telegram_id: int) -> int:
        return await asyncio.to_thread(self.database.decline_alternatives, request_id, telegram_id)

    async def create_change_request(
        self,
        *,
        request_id: int,
        telegram_id: int,
        change_type: str,
        start_at: datetime | None,
        duration_minutes: int | None,
    ) -> ChangeRequest:
        if change_type == CHANGE_CANCEL:
            if start_at is not None or duration_minutes is not None:
                raise BookingValidationError("cancel change cannot include a slot")
            return await asyncio.to_thread(self.database.create_change_request, request_id, telegram_id, CHANGE_CANCEL)
        if change_type != CHANGE_RESCHEDULE or start_at is None or duration_minutes is None:
            raise BookingValidationError("reschedule requires a slot")
        start_utc, end_utc = self._validate_slot(start_at, duration_minutes, self.rules())
        if not await self.calendar.is_free(start_utc, end_utc):
            raise SlotConflictError("google calendar slot is busy")
        return await asyncio.to_thread(
            self.database.create_change_request,
            request_id,
            telegram_id,
            CHANGE_RESCHEDULE,
            start_utc,
            end_utc,
        )

    async def create_deletion_request(self, telegram_id: int, mode: str) -> DeletionRequest:
        return await asyncio.to_thread(self.deletions.create_deletion_request, telegram_id, mode)

    async def confirm_deletion_request(self, request_id: int, telegram_id: int) -> DeletionRequest:
        request = await asyncio.to_thread(self.deletions.get_deletion_request, request_id)
        if request is None or request.telegram_id != telegram_id or request.status != DELETE_REQUESTED:
            raise RequestNotEditableError("deletion request is unavailable")
        if request.mode == DELETE_CANCEL_FUTURE:
            try:
                for _, event_id in await asyncio.to_thread(self.deletions.future_google_events, request):
                    await self.calendar.delete_event(event_id)
            except Exception as exc:
                await asyncio.to_thread(self.deletions.mark_deletion_failed, request.id, type(exc).__name__)
                raise CalendarUnavailable("calendar deletion failed") from exc
            await asyncio.to_thread(self.deletions.complete_cancel_future, request.id, telegram_id)
        elif request.mode == DELETE_KEEP_FUTURE:
            await asyncio.to_thread(self.deletions.complete_keep_future, request.id, telegram_id)
        else:
            raise RequestNotEditableError("deletion mode is unavailable")
        completed = await asyncio.to_thread(self.deletions.get_deletion_request, request_id)
        if completed is None or completed.status not in {DELETE_COMPLETED, DELETE_WAITING}:
            raise RuntimeError("deletion request did not complete")
        return completed

    def _validate_date_and_duration(self, selected_date: date, duration_minutes: int, rules: BookingRules) -> None:
        today = datetime.now(self.zone).date()
        if not rules.booking_enabled or duration_minutes not in rules.durations:
            raise BookingValidationError("booking settings do not allow this duration")
        if selected_date < today or selected_date >= today + timedelta(days=rules.booking_horizon_days):
            raise BookingValidationError("date is outside booking horizon")

    def _validate_slot(self, start_at: datetime, duration_minutes: int, rules: BookingRules) -> tuple[datetime, datetime]:
        if start_at.tzinfo is None:
            raise BookingValidationError("start_at must include timezone")
        local_start = start_at.astimezone(self.zone)
        self._validate_date_and_duration(local_start.date(), duration_minutes, rules)
        local_end = local_start + timedelta(minutes=duration_minutes)
        minute = local_start.hour * 60 + local_start.minute
        if minute % rules.step_minutes != 0:
            raise BookingValidationError("slot does not match booking step")
        if (
            minute < rules.user_booking_start_minutes
            or minute + duration_minutes > rules.user_booking_end_minutes
            or local_start < datetime.now(self.zone) + timedelta(minutes=rules.min_lead_minutes)
            or local_start.date().isoformat() in set(self.database.list_closed_dates(local_start.date().isoformat(), 1))
        ):
            raise BookingValidationError("slot is outside booking rules")
        return local_start.astimezone(UTC), local_end.astimezone(UTC)

    @staticmethod
    def _optional(value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @staticmethod
    def _validate_request_fields(name: str, email: str, subject: str, description: str | None, location: str | None) -> None:
        if not name.strip() or len(name.strip()) > 120:
            raise BookingValidationError("invalid name")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip()) or len(email.strip()) > 254:
            raise BookingValidationError("invalid email")
        if not subject.strip() or len(subject.strip()) > 200:
            raise BookingValidationError("invalid subject")
        if description and len(description) > 4000:
            raise BookingValidationError("description is too long")
        if location and len(location) > 1000:
            raise BookingValidationError("location is too long")


__all__ = [
    "BookingValidationError", "CalendarUnavailable", "ConsentRequiredError", "IdempotencyConflictError",
    "MiniAppBookingService", "RequestNotEditableError", "SlotConflictError",
]

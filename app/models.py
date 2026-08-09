from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


PENDING = "PENDING"
APPROVING = "APPROVING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
CANCELLED_BY_ADMIN = "CANCELLED_BY_ADMIN"

ALTERNATIVE_OFFERED = "OFFERED"
ALTERNATIVE_ACCEPTED = "ACCEPTED"
ALTERNATIVE_DECLINED = "DECLINED"
ALTERNATIVE_WITHDRAWN = "WITHDRAWN"

CHANGE_CANCEL = "CANCEL"
CHANGE_RESCHEDULE = "RESCHEDULE"
CHANGE_PENDING = "PENDING"
CHANGE_APPROVING = "APPROVING"
CHANGE_APPROVED = "APPROVED"
CHANGE_REJECTED = "REJECTED"
CHANGE_FAILED = "FAILED"

SERIES_DAILY = "DAILY"
SERIES_WEEKLY = "WEEKLY"
SERIES_MONTHLY = "MONTHLY"
SERIES_CREATING = "CREATING"
SERIES_ACTIVE = "ACTIVE"
SERIES_CANCELLED = "CANCELLED"
SERIES_FAILED = "FAILED"

OCCURRENCE_SCHEDULED = "SCHEDULED"
OCCURRENCE_MOVED = "MOVED"
OCCURRENCE_CANCELLED = "CANCELLED"
OCCURRENCE_MISSING = "MISSING"

JOB_MEETING_REMINDER = "MEETING_REMINDER"
JOB_PENDING_REMINDER = "PENDING_REMINDER"
JOB_NEW_REQUEST_NOTIFICATION = "NEW_REQUEST_NOTIFICATION"
JOB_PENDING = "PENDING"
JOB_PROCESSING = "PROCESSING"
JOB_DONE = "DONE"
JOB_CANCELLED = "CANCELLED"
JOB_FAILED = "FAILED"

SYNCED = "SYNCED"
SYNC_CHANGED = "CHANGED"
SYNC_MISSING = "MISSING"
SYNC_ERROR = "ERROR"


@dataclass(frozen=True)
class MeetingRequest:
    id: int
    telegram_id: int
    telegram_name: str
    telegram_username: str | None
    email: str
    subject: str
    description: str | None
    location: str | None
    start_at: datetime
    end_at: datetime
    status: str
    hold_until: datetime
    google_event_id: str | None
    created_at: datetime
    updated_at: datetime
    source: str = "USER"
    blocks_calendar: bool = True
    admin_override: bool = False


@dataclass(frozen=True)
class RequestAlternative:
    id: int
    request_id: int
    start_at: datetime
    end_at: datetime
    status: str
    hold_until: datetime
    created_by: int
    created_at: datetime
    responded_at: datetime | None


@dataclass(frozen=True)
class ChangeRequest:
    id: int
    request_id: int
    change_type: str
    proposed_start_at: datetime | None
    proposed_end_at: datetime | None
    status: str
    requested_by: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventSeries:
    id: int
    created_by: int
    admin_name: str
    admin_username: str | None
    email: str | None
    subject: str
    description: str | None
    location: str | None
    start_at: datetime
    end_at: datetime
    frequency: str
    until_date: str
    status: str
    google_series_id: str | None
    blocks_calendar: bool
    allow_overlap: bool
    sync_state: str
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventOccurrence:
    id: int
    series_id: int
    expected_start_at: datetime
    expected_end_at: datetime
    actual_start_at: datetime
    actual_end_at: datetime
    status: str
    google_event_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ScheduledJob:
    id: int
    job_type: str
    request_id: int | None
    occurrence_id: int | None
    recipient_telegram_id: int
    due_at: datetime
    status: str
    attempt_count: int
    max_attempts: int
    idempotency_key: str
    claimed_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CalendarEventState:
    exists: bool
    event_id: str
    start_at: datetime | None
    end_at: datetime | None
    subject: str | None
    description: str | None
    location: str | None
    blocks_calendar: bool | None
    updated_at: datetime | None


@dataclass(frozen=True)
class MiniAppSession:
    telegram_id: int
    csrf_hash: str
    expires_at: datetime

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

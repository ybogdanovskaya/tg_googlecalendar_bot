from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


PENDING = "PENDING"
APPROVING = "APPROVING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"


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

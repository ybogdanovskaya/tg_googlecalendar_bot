from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.models import MeetingRequest


class CalendarUnavailable(RuntimeError):
    pass


class CalendarClient(Protocol):
    async def busy(self, start_at: datetime, end_at: datetime) -> list[tuple[datetime, datetime]]: ...

    async def is_free(self, start_at: datetime, end_at: datetime) -> bool: ...

    async def create_event(self, request: MeetingRequest) -> str: ...

    async def update_event(self, request: MeetingRequest) -> str: ...

    async def delete_event(self, event_id: str) -> bool: ...

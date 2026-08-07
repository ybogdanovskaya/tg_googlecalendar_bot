from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.models import CalendarEventState, EventOccurrence, EventSeries, MeetingRequest


class CalendarUnavailable(RuntimeError):
    pass


class CalendarClient(Protocol):
    async def busy(self, start_at: datetime, end_at: datetime) -> list[tuple[datetime, datetime]]: ...

    async def is_free(self, start_at: datetime, end_at: datetime) -> bool: ...

    async def create_event(self, request: MeetingRequest) -> str: ...

    async def update_event(self, request: MeetingRequest) -> str: ...

    async def delete_event(self, event_id: str) -> bool: ...

    async def event_state(self, event_id: str) -> CalendarEventState: ...

    async def create_series(self, series: EventSeries) -> str: ...

    async def update_series(self, series: EventSeries) -> str: ...

    async def delete_series(self, series_id: str) -> bool: ...

    async def update_occurrence(
        self,
        series_id: str,
        occurrence: EventOccurrence,
        start_at: datetime,
        end_at: datetime,
    ) -> str: ...

    async def delete_occurrence(self, series_id: str, occurrence: EventOccurrence) -> bool: ...

    async def occurrence_state(
        self,
        series_id: str,
        occurrence: EventOccurrence,
    ) -> CalendarEventState: ...

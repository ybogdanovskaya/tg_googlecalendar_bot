from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.calendar_client import CalendarUnavailable
from app.models import CalendarEventState, EventOccurrence, EventSeries, MeetingRequest


LOGGER = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleCalendar:
    def __init__(self, token_file: Path, calendar_id: str) -> None:
        self.token_file = token_file
        self.calendar_id = calendar_id

    def _service(self) -> Any:
        if not self.token_file.exists():
            raise CalendarUnavailable(f"Google token file not found: {self.token_file}")
        credentials = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self.token_file.write_text(credentials.to_json(), encoding="utf-8")
        if not credentials.valid:
            raise CalendarUnavailable("Google credentials are invalid")
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    async def busy(self, start_at: datetime, end_at: datetime) -> list[tuple[datetime, datetime]]:
        return await asyncio.to_thread(self._busy_sync, start_at, end_at)

    def _busy_sync(self, start_at: datetime, end_at: datetime) -> list[tuple[datetime, datetime]]:
        try:
            service = self._service()
            response = service.freebusy().query(
                body={
                    "timeMin": start_at.astimezone(UTC).isoformat(),
                    "timeMax": end_at.astimezone(UTC).isoformat(),
                    "items": [{"id": self.calendar_id}],
                }
            ).execute()
            calendar_data = response.get("calendars", {}).get(self.calendar_id, {})
            if calendar_data.get("errors"):
                raise CalendarUnavailable(str(calendar_data["errors"]))
            return [
                (
                    datetime.fromisoformat(item["start"].replace("Z", "+00:00")),
                    datetime.fromisoformat(item["end"].replace("Z", "+00:00")),
                )
                for item in calendar_data.get("busy", [])
            ]
        except CalendarUnavailable:
            raise
        except Exception as exc:
            LOGGER.exception("google_freebusy_failed")
            raise CalendarUnavailable("Google Calendar temporarily unavailable") from exc

    async def is_free(self, start_at: datetime, end_at: datetime) -> bool:
        return not await self.busy(start_at, end_at)

    async def create_event(self, request: MeetingRequest) -> str:
        return await asyncio.to_thread(self._create_event_sync, request)

    def _create_event_sync(self, request: MeetingRequest) -> str:
        event_id = f"tgmeet{request.id:x}"
        description_parts = [
            f"Заявка из Telegram №{request.id}",
            f"Участник: {request.telegram_name}",
        ]
        if request.telegram_username:
            description_parts.append(f"Telegram: @{request.telegram_username}")
        if request.description:
            description_parts.append("")
            description_parts.append(request.description)
        body: dict[str, Any] = {
            "id": event_id,
            "summary": request.subject,
            "description": "\n".join(description_parts),
            "start": {"dateTime": request.start_at.astimezone(UTC).isoformat()},
            "end": {"dateTime": request.end_at.astimezone(UTC).isoformat()},
            "guestsCanModify": False,
            "transparency": "opaque" if request.blocks_calendar else "transparent",
            "extendedProperties": {"private": {"telegram_request_id": str(request.id)}},
        }
        if request.email:
            body["attendees"] = [{"email": request.email}]
        if request.location:
            body["location"] = request.location
        try:
            service = self._service()
            event = service.events().insert(
                calendarId=self.calendar_id,
                body=body,
                sendUpdates="all",
            ).execute()
            return str(event["id"])
        except HttpError as exc:
            if exc.resp.status == 409:
                try:
                    existing = self._service().events().get(
                        calendarId=self.calendar_id,
                        eventId=event_id,
                    ).execute()
                    return str(existing["id"])
                except Exception as nested:
                    raise CalendarUnavailable("Cannot verify existing Google event") from nested
            LOGGER.exception("google_event_create_failed", extra={"request_id": request.id})
            raise CalendarUnavailable("Google event creation failed") from exc
        except Exception as exc:
            LOGGER.exception("google_event_create_failed", extra={"request_id": request.id})
            raise CalendarUnavailable("Google event creation failed") from exc

    async def update_event(self, request: MeetingRequest) -> str:
        return await asyncio.to_thread(self._update_event_sync, request)

    def _update_event_sync(self, request: MeetingRequest) -> str:
        if not request.google_event_id:
            raise CalendarUnavailable("Meeting has no Google event ID")
        body: dict[str, Any] = {
            "summary": request.subject,
            "description": request.description or "",
            "start": {"dateTime": request.start_at.astimezone(UTC).isoformat()},
            "end": {"dateTime": request.end_at.astimezone(UTC).isoformat()},
            "location": request.location or "",
            "transparency": "opaque" if request.blocks_calendar else "transparent",
        }
        try:
            event = self._service().events().patch(
                calendarId=self.calendar_id,
                eventId=request.google_event_id,
                body=body,
                sendUpdates="all",
            ).execute()
            return str(event["id"])
        except Exception as exc:
            LOGGER.exception("google_event_update_failed", extra={"request_id": request.id})
            raise CalendarUnavailable("Google event update failed") from exc

    async def delete_event(self, event_id: str) -> bool:
        return await asyncio.to_thread(self._delete_event_sync, event_id)

    def _delete_event_sync(self, event_id: str) -> bool:
        try:
            self._service().events().delete(
                calendarId=self.calendar_id,
                eventId=event_id,
                sendUpdates="all",
            ).execute()
            return True
        except HttpError as exc:
            if exc.resp.status in {404, 410}:
                return False
            LOGGER.exception("google_event_delete_failed", extra={"event_id": event_id})
            raise CalendarUnavailable("Google event deletion failed") from exc

    async def event_state(self, event_id: str) -> CalendarEventState:
        return await asyncio.to_thread(self._event_state_sync, event_id)

    def _event_state_sync(self, event_id: str) -> CalendarEventState:
        try:
            item = self._service().events().get(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status in {404, 410}:
                return CalendarEventState(False, event_id, None, None, None, None, None, None, None)
            raise CalendarUnavailable("Google event status failed") from exc
        try:
            start_at = datetime.fromisoformat(str(item["start"]["dateTime"]).replace("Z", "+00:00")).astimezone(UTC)
            end_at = datetime.fromisoformat(str(item["end"]["dateTime"]).replace("Z", "+00:00")).astimezone(UTC)
            updated = datetime.fromisoformat(str(item["updated"]).replace("Z", "+00:00")).astimezone(UTC) if item.get("updated") else None
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarUnavailable("Invalid Google event status") from exc
        return CalendarEventState(
            True,
            event_id,
            start_at,
            end_at,
            str(item.get("summary") or ""),
            str(item.get("description") or ""),
            str(item.get("location") or ""),
            str(item.get("transparency") or "opaque") != "transparent",
            updated,
        )

    async def create_series(self, series: EventSeries) -> str:
        return await asyncio.to_thread(self._create_series_sync, series)

    def _series_body(self, series: EventSeries) -> dict[str, Any]:
        until = series.until_date.replace("-", "") + "T205959Z"
        body: dict[str, Any] = {
            "summary": series.subject,
            "description": series.description or "",
            "location": series.location or "",
            "start": {"dateTime": series.start_at.astimezone(UTC).isoformat()},
            "end": {"dateTime": series.end_at.astimezone(UTC).isoformat()},
            "recurrence": [f"RRULE:FREQ={series.frequency};UNTIL={until}"],
            "transparency": "opaque" if series.blocks_calendar else "transparent",
            "guestsCanModify": False,
            "extendedProperties": {"private": {"telegram_series_id": str(series.id)}},
        }
        if series.email:
            body["attendees"] = [{"email": series.email}]
        return body

    def _create_series_sync(self, series: EventSeries) -> str:
        try:
            item = self._service().events().insert(
                calendarId=self.calendar_id,
                body=self._series_body(series),
                sendUpdates="all",
            ).execute()
            return str(item["id"])
        except Exception as exc:
            LOGGER.exception("google_series_create_failed", extra={"series_id": series.id})
            raise CalendarUnavailable("Google series creation failed") from exc

    async def update_series(self, series: EventSeries) -> str:
        if not series.google_series_id:
            raise CalendarUnavailable("Series has no Google ID")
        return await asyncio.to_thread(self._update_series_sync, series)

    def _update_series_sync(self, series: EventSeries) -> str:
        try:
            item = self._service().events().patch(
                calendarId=self.calendar_id,
                eventId=series.google_series_id,
                body=self._series_body(series),
                sendUpdates="all",
            ).execute()
            return str(item["id"])
        except Exception as exc:
            LOGGER.exception("google_series_update_failed", extra={"series_id": series.id})
            raise CalendarUnavailable("Google series update failed") from exc

    async def delete_series(self, series_id: str) -> bool:
        return await self.delete_event(series_id)

    def _instance_id(self, series_id: str, lookup_start: datetime) -> str | None:
        response = self._service().events().instances(
            calendarId=self.calendar_id,
            eventId=series_id,
            timeMin=(lookup_start - timedelta(minutes=2)).astimezone(UTC).isoformat(),
            timeMax=(lookup_start + timedelta(minutes=2)).astimezone(UTC).isoformat(),
            showDeleted=False,
            maxResults=5,
        ).execute()
        items = response.get("items", [])
        return str(items[0]["id"]) if items else None

    async def update_occurrence(
        self,
        series_id: str,
        occurrence: EventOccurrence,
        start_at: datetime,
        end_at: datetime,
    ) -> str:
        return await asyncio.to_thread(self._update_occurrence_sync, series_id, occurrence, start_at, end_at)

    def _update_occurrence_sync(
        self,
        series_id: str,
        occurrence: EventOccurrence,
        start_at: datetime,
        end_at: datetime,
    ) -> str:
        try:
            instance_id = self._instance_id(series_id, occurrence.actual_start_at)
            if not instance_id:
                raise CalendarUnavailable("Google occurrence not found")
            item = self._service().events().patch(
                calendarId=self.calendar_id,
                eventId=instance_id,
                body={
                    "start": {"dateTime": start_at.astimezone(UTC).isoformat()},
                    "end": {"dateTime": end_at.astimezone(UTC).isoformat()},
                },
                sendUpdates="all",
            ).execute()
            return str(item["id"])
        except CalendarUnavailable:
            raise
        except Exception as exc:
            raise CalendarUnavailable("Google occurrence update failed") from exc

    async def delete_occurrence(self, series_id: str, occurrence: EventOccurrence) -> bool:
        return await asyncio.to_thread(self._delete_occurrence_sync, series_id, occurrence)

    def _delete_occurrence_sync(self, series_id: str, occurrence: EventOccurrence) -> bool:
        try:
            instance_id = self._instance_id(series_id, occurrence.actual_start_at)
            if not instance_id:
                return False
            self._service().events().delete(
                calendarId=self.calendar_id,
                eventId=instance_id,
                sendUpdates="all",
            ).execute()
            return True
        except HttpError as exc:
            if exc.resp.status in {404, 410}:
                return False
            raise CalendarUnavailable("Google occurrence deletion failed") from exc
        except Exception as exc:
            raise CalendarUnavailable("Google occurrence deletion failed") from exc
        except Exception as exc:
            LOGGER.exception("google_event_delete_failed", extra={"event_id": event_id})
            raise CalendarUnavailable("Google event deletion failed") from exc

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.calendar_client import CalendarUnavailable
from app.models import MeetingRequest


LOGGER = logging.getLogger(__name__)


class AppsScriptCalendar:
    def __init__(self, url: str, secret_file: Path, timeout_seconds: int = 20) -> None:
        if not url.startswith("https://script.google.com/") or not url.endswith("/exec"):
            raise RuntimeError("Invalid Google Apps Script deployment URL")
        self.url = url
        self.secret_file = secret_file
        self.timeout_seconds = timeout_seconds

    def _secret(self) -> str:
        if not self.secret_file.exists():
            raise CalendarUnavailable("Apps Script secret file not found")
        value = self.secret_file.read_text(encoding="utf-8").strip()
        if not value:
            raise CalendarUnavailable("Apps Script secret is empty")
        return value

    async def busy(self, start_at: datetime, end_at: datetime) -> list[tuple[datetime, datetime]]:
        response = await asyncio.to_thread(
            self._post,
            {
                "action": "busy",
                "start": start_at.astimezone(UTC).isoformat(),
                "end": end_at.astimezone(UTC).isoformat(),
            },
        )
        intervals = response.get("busy")
        if not isinstance(intervals, list):
            raise CalendarUnavailable("Invalid busy response from Apps Script")
        result: list[tuple[datetime, datetime]] = []
        try:
            for item in intervals:
                result.append(
                    (
                        datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00")).astimezone(UTC),
                        datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00")).astimezone(UTC),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarUnavailable("Invalid interval from Apps Script") from exc
        return result

    async def is_free(self, start_at: datetime, end_at: datetime) -> bool:
        return not await self.busy(start_at, end_at)

    async def create_event(self, request: MeetingRequest) -> str:
        description_parts = [
            f"Заявка из Telegram №{request.id}",
            f"Участник: {request.telegram_name}",
        ]
        if request.telegram_username:
            description_parts.append(f"Telegram: @{request.telegram_username}")
        if request.description:
            description_parts.extend(["", request.description])
        response = await asyncio.to_thread(
            self._post,
            {
                "action": "create",
                "requestId": str(request.id),
                "start": request.start_at.astimezone(UTC).isoformat(),
                "end": request.end_at.astimezone(UTC).isoformat(),
                "subject": request.subject,
                "email": request.email,
                "description": "\n".join(description_parts),
                "location": request.location or "",
                "allowOverlap": request.admin_override,
                "transparent": not request.blocks_calendar,
            },
        )
        event_id = response.get("eventId")
        if not isinstance(event_id, str) or not event_id:
            raise CalendarUnavailable("Apps Script did not return an event ID")
        return event_id

    async def update_event(self, request: MeetingRequest) -> str:
        if not request.google_event_id:
            raise CalendarUnavailable("Meeting has no Google event ID")
        response = await asyncio.to_thread(
            self._post,
            {
                "action": "update",
                "eventId": request.google_event_id,
                "start": request.start_at.astimezone(UTC).isoformat(),
                "end": request.end_at.astimezone(UTC).isoformat(),
                "subject": request.subject,
                "description": request.description or "",
                "location": request.location or "",
                "allowOverlap": request.admin_override,
                "transparent": not request.blocks_calendar,
            },
        )
        event_id = response.get("eventId")
        if not isinstance(event_id, str) or not event_id:
            raise CalendarUnavailable("Apps Script did not return an updated event ID")
        return event_id

    async def delete_event(self, event_id: str) -> bool:
        response = await asyncio.to_thread(
            self._post,
            {"action": "delete", "eventId": event_id},
        )
        deleted = response.get("deleted")
        if not isinstance(deleted, bool):
            raise CalendarUnavailable("Apps Script did not return deletion status")
        return deleted

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body["secret"] = self._secret()
        request = Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            LOGGER.exception("apps_script_request_failed")
            raise CalendarUnavailable("Google Apps Script is unavailable") from exc
        if not isinstance(decoded, dict) or decoded.get("ok") is not True:
            error = decoded.get("error", "unknown_error") if isinstance(decoded, dict) else "invalid_response"
            LOGGER.error("apps_script_error", extra={"apps_script_error": error})
            raise CalendarUnavailable(f"Apps Script error: {error}")
        return decoded

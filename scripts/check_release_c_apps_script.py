from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description="Check release C Google Apps Script operations")
    parser.add_argument("--url-file", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    args = parser.parse_args()
    url = args.url_file.read_text(encoding="utf-8").strip()
    secret = args.secret_file.read_text(encoding="utf-8").strip()

    def post(payload: dict[str, object]) -> dict[str, object]:
        body = dict(payload)
        body["secret"] = secret
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = result.get("error", "invalid_response") if isinstance(result, dict) else "invalid_response"
            raise RuntimeError(str(error))
        return result

    def wait_for_occurrence(payload: dict[str, object], expected_exists: bool) -> dict[str, object]:
        last: dict[str, object] = {}
        for attempt in range(4):
            last = post(payload)
            if last.get("exists") is expected_exists:
                return last
            if attempt < 3:
                time.sleep(3)
        return last

    # Прозрачная тестовая серия создаётся далеко за рабочим горизонтом и удаляется в finally.
    start = datetime.now(UTC) + timedelta(days=300)
    start = start.replace(hour=8, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=15)
    until = (start + timedelta(days=2)).date().isoformat()
    series_key = "release-c-check-" + uuid.uuid4().hex
    series_id = ""
    try:
        created = post(
            {
                "action": "seriesCreate",
                "seriesId": series_key,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "untilDate": until,
                "frequency": "DAILY",
                "subject": "[ТЕСТ] Проверка релиза C",
                "email": "",
                "description": "Автоматическая проверка; серия будет удалена.",
                "location": "",
                "allowOverlap": True,
                "transparent": True,
            }
        )
        series_id = str(created.get("eventId") or "")
        if not series_id:
            raise RuntimeError("series_create_did_not_return_event_id")

        shifted = start + timedelta(minutes=15)
        updated = post(
            {
                "action": "seriesUpdate",
                "eventId": series_id,
                "start": shifted.isoformat(),
                "end": (shifted + timedelta(minutes=15)).isoformat(),
                "untilDate": until,
                "frequency": "DAILY",
                "subject": "[ТЕСТ] Изменённая серия релиза C",
                "description": "Автоматическая проверка; серия будет удалена.",
                "location": "",
                "transparent": True,
            }
        )
        if str(updated.get("eventId") or "") != series_id:
            raise RuntimeError("series_update_returned_another_event_id")

        initial_state = wait_for_occurrence(
            {
                "action": "occurrenceStatus",
                "eventId": series_id,
                "lookupStart": shifted.isoformat(),
                "expectedStart": shifted.isoformat(),
            },
            True,
        )
        if initial_state.get("exists") is not True:
            raise RuntimeError("first_occurrence_not_found")

        moved = shifted + timedelta(minutes=30)
        occurrence_update = post(
            {
                "action": "occurrenceUpdate",
                "eventId": series_id,
                "lookupStart": shifted.isoformat(),
                "start": moved.isoformat(),
                "end": (moved + timedelta(minutes=15)).isoformat(),
            }
        )
        if not str(occurrence_update.get("eventId") or ""):
            raise RuntimeError("occurrence_update_did_not_return_event_id")
        moved_state = wait_for_occurrence(
            {
                "action": "occurrenceStatus",
                "eventId": series_id,
                "lookupStart": moved.isoformat(),
                "expectedStart": shifted.isoformat(),
            },
            True,
        )
        if moved_state.get("exists") is not True or str(moved_state.get("start") or "") != moved.isoformat().replace("+00:00", "Z"):
            raise RuntimeError("moved_occurrence_status_is_invalid")

        second = shifted + timedelta(days=1)
        deleted = post(
            {
                "action": "occurrenceDelete",
                "eventId": series_id,
                "lookupStart": second.isoformat(),
            }
        )
        if deleted.get("deleted") is not True:
            raise RuntimeError("occurrence_delete_was_not_confirmed")
        deleted_state = wait_for_occurrence(
            {
                "action": "occurrenceStatus",
                "eventId": series_id,
                "lookupStart": second.isoformat(),
                "expectedStart": second.isoformat(),
            },
            False,
        )
        if deleted_state.get("exists") is not False:
            raise RuntimeError("deleted_occurrence_still_exists")

        series_deleted = post({"action": "seriesDelete", "eventId": series_id})
        if series_deleted.get("deleted") is not True:
            raise RuntimeError("series_delete_was_not_confirmed")
        series_id = ""
        print("ok series_create=true series_update=true occurrence_update=true occurrence_delete=true reconciliation=true")
    finally:
        if series_id:
            try:
                post({"action": "seriesDelete", "eventId": series_id})
            except Exception:
                pass


if __name__ == "__main__":
    main()

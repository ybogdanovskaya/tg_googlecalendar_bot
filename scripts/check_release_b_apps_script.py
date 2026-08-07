from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description="Check release B Google Apps Script operations")
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
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = result.get("error", "invalid_response") if isinstance(result, dict) else "invalid_response"
            raise RuntimeError(str(error))
        return result

    # Далёкое прозрачное событие не влияет на рабочие слоты и удаляется в finally.
    start = datetime.now(UTC) + timedelta(days=400)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=15)
    request_id = "release-b-check-" + uuid.uuid4().hex
    event_id = ""
    try:
        created = post(
            {
                "action": "create",
                "requestId": request_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "subject": "[ТЕСТ] Проверка релиза B",
                "email": "",
                "description": "Автоматическая проверка; событие будет удалено.",
                "location": "",
                "allowOverlap": True,
                "transparent": True,
            }
        )
        event_id = str(created.get("eventId") or "")
        if not event_id:
            raise RuntimeError("create_did_not_return_event_id")
        updated = post(
            {
                "action": "update",
                "eventId": event_id,
                "start": (start + timedelta(minutes=15)).isoformat(),
                "end": (end + timedelta(minutes=15)).isoformat(),
                "subject": "[ТЕСТ] Проверка изменения релиза B",
                "description": "Автоматическая проверка; событие будет удалено.",
                "location": "",
                "allowOverlap": True,
                "transparent": True,
            }
        )
        if str(updated.get("eventId") or "") != event_id:
            raise RuntimeError("update_returned_another_event_id")
        deleted = post({"action": "delete", "eventId": event_id})
        if deleted.get("deleted") is not True:
            raise RuntimeError("delete_was_not_confirmed")
        event_id = ""
        print("ok create=true update=true delete=true")
    finally:
        if event_id:
            try:
                post({"action": "delete", "eventId": event_id})
            except Exception:
                pass


if __name__ == "__main__":
    main()

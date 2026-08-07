from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.apps_script_calendar import AppsScriptCalendar
from app.automation_store import AutomationStore
from app.calendar_client import CalendarUnavailable
from app.db import Database
from app.models import SERIES_CREATING, SERIES_FAILED


async def recover(args: argparse.Namespace) -> None:
    database = Database(args.database)
    store = AutomationStore(database)
    series = store.get_series(args.series_id)
    if series is None or series.status not in {SERIES_FAILED, SERIES_CREATING} or series.google_series_id:
        raise SystemExit("series_is_not_recoverable")
    if series.status == SERIES_FAILED:
        series = store.retry_series_draft(series.id, series.created_by)
    calendar = AppsScriptCalendar(
        args.url_file.read_text(encoding="utf-8").strip(),
        args.secret_file,
    )
    try:
        google_id = ""
        for attempt in range(2):
            try:
                google_id = await calendar.create_series(series)
                break
            except CalendarUnavailable:
                if attempt:
                    raise
                await asyncio.sleep(2)
        active = store.activate_series(series.id, google_id, series.created_by)
        reminders = 0
        for occurrence in store.list_occurrences(active.id, future_only=True, limit=400):
            reminders += store.rebuild_occurrence_reminders(
                occurrence.id,
                active.created_by,
                (1440, 60, 5),
            )
    except Exception as exc:
        store.fail_series(series.id, type(exc).__name__)
        raise
    print(
        json.dumps(
            {
                "ok": True,
                "series_id": active.id,
                "status": active.status,
                "occurrences": len(store.list_occurrences(active.id)),
                "reminder_jobs": reminders,
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover an idempotently created Google series")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--url-file", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--series-id", type=int, required=True)
    asyncio.run(recover(parser.parse_args()))


if __name__ == "__main__":
    main()

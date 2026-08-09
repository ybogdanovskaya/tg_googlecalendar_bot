from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Iterable

from app.db import Database, RequestNotEditableError, SlotConflictError, _dt, _iso
from app.models import (
    ALTERNATIVE_OFFERED,
    APPROVED,
    APPROVING,
    CANCELLED_BY_ADMIN,
    CHANGE_APPROVING,
    CHANGE_PENDING,
    CHANGE_RESCHEDULE,
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_MEETING_REMINDER,
    JOB_NEW_REQUEST_NOTIFICATION,
    JOB_PENDING,
    JOB_PENDING_REMINDER,
    JOB_PROCESSING,
    OCCURRENCE_CANCELLED,
    OCCURRENCE_MISSING,
    OCCURRENCE_MOVED,
    OCCURRENCE_SCHEDULED,
    PENDING,
    SERIES_ACTIVE,
    SERIES_CANCELLED,
    SERIES_CREATING,
    SERIES_FAILED,
    SYNC_CHANGED,
    SYNC_MISSING,
    SYNCED,
    CalendarEventState,
    EventOccurrence,
    EventSeries,
    MeetingRequest,
    ScheduledJob,
)


class AutomationStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create_series_draft(
        self,
        *,
        admin_id: int,
        admin_name: str,
        admin_username: str | None,
        email: str | None,
        subject: str,
        description: str | None,
        location: str | None,
        start_at: datetime,
        end_at: datetime,
        frequency: str,
        until_date: str,
        blocks_calendar: bool,
        allow_overlap: bool,
        occurrences: list[tuple[datetime, datetime]],
    ) -> EventSeries:
        if not occurrences:
            raise ValueError("series requires occurrences")
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not allow_overlap:
                for occurrence_start, occurrence_end in occurrences:
                    if self._slot_conflict(connection, occurrence_start, occurrence_end):
                        raise SlotConflictError("series occurrence overlaps")
            cursor = connection.execute(
                """
                INSERT INTO event_series (
                    created_by, admin_name, admin_username, email, subject, description,
                    location, start_at, end_at, frequency, until_date, status,
                    blocks_calendar, allow_overlap, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admin_id,
                    admin_name,
                    admin_username,
                    email,
                    subject,
                    description,
                    location,
                    _iso(start_at),
                    _iso(end_at),
                    frequency,
                    until_date,
                    SERIES_CREATING,
                    int(blocks_calendar),
                    int(allow_overlap),
                    now,
                    now,
                ),
            )
            series_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO event_occurrences (
                    series_id, expected_start_at, expected_end_at, actual_start_at,
                    actual_end_at, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        series_id,
                        _iso(item_start),
                        _iso(item_end),
                        _iso(item_start),
                        _iso(item_end),
                        OCCURRENCE_SCHEDULED,
                        now,
                        now,
                    )
                    for item_start, item_end in occurrences
                ],
            )
            self.db._audit(
                connection,
                admin_id,
                "series_create_started",
                "event_series",
                str(series_id),
                {"frequency": frequency, "occurrences": len(occurrences), "allow_overlap": allow_overlap},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        series = self.get_series(series_id)
        if series is None:
            raise RuntimeError("series draft not found")
        return series

    def activate_series(self, series_id: int, google_series_id: str, admin_id: int) -> EventSeries:
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE event_series
                SET status = ?, google_series_id = ?, sync_state = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (SERIES_ACTIVE, google_series_id, SYNCED, now, series_id, SERIES_CREATING),
            )
            if cursor.rowcount != 1:
                raise RequestNotEditableError("series is not being created")
            connection.execute(
                "UPDATE event_occurrences SET google_event_id = ?, updated_at = ? WHERE series_id = ?",
                (google_series_id, now, series_id),
            )
            self.db._audit(connection, admin_id, "series_created", "event_series", str(series_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_series(series_id)
        if result is None:
            raise RuntimeError("active series not found")
        return result

    def fail_series(self, series_id: int, reason: str) -> None:
        now = _iso(datetime.now(UTC))
        with self.db._connect() as connection:
            connection.execute(
                "UPDATE event_series SET status = ?, sync_state = ?, updated_at = ? WHERE id = ? AND status = ?",
                (SERIES_FAILED, reason, now, series_id, SERIES_CREATING),
            )
            connection.execute(
                "UPDATE event_occurrences SET status = ?, updated_at = ? WHERE series_id = ?",
                (OCCURRENCE_CANCELLED, now, series_id),
            )
            self.db._audit(connection, None, "series_create_failed", "event_series", str(series_id), {"reason": reason})

    def retry_series_draft(self, series_id: int, admin_id: int) -> EventSeries:
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE event_series
                SET status = ?, sync_state = ?, updated_at = ?
                WHERE id = ? AND created_by = ? AND status = ? AND google_series_id IS NULL
                """,
                (SERIES_CREATING, SYNCED, now, series_id, admin_id, SERIES_FAILED),
            )
            if cursor.rowcount != 1:
                raise RequestNotEditableError("failed series draft not found")
            connection.execute(
                """
                UPDATE event_occurrences SET status = ?, updated_at = ?
                WHERE series_id = ? AND status = ?
                """,
                (OCCURRENCE_SCHEDULED, now, series_id, OCCURRENCE_CANCELLED),
            )
            self.db._audit(connection, admin_id, "series_create_retried", "event_series", str(series_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_series(series_id)
        if result is None:
            raise RuntimeError("retried series draft not found")
        return result

    def get_series(self, series_id: int) -> EventSeries | None:
        with self.db._connect() as connection:
            row = connection.execute("SELECT * FROM event_series WHERE id = ?", (series_id,)).fetchone()
        return self._row_to_series(row) if row else None

    def list_series(self, admin_id: int, limit: int = 20) -> list[EventSeries]:
        with self.db._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_series
                WHERE created_by = ? AND status IN (?, ?)
                ORDER BY created_at DESC LIMIT ?
                """,
                (admin_id, SERIES_ACTIVE, SERIES_CREATING, limit),
            ).fetchall()
        return [self._row_to_series(row) for row in reversed(rows)]

    def get_occurrence(self, occurrence_id: int) -> EventOccurrence | None:
        with self.db._connect() as connection:
            row = connection.execute("SELECT * FROM event_occurrences WHERE id = ?", (occurrence_id,)).fetchone()
        return self._row_to_occurrence(row) if row else None

    def list_occurrences(
        self,
        series_id: int,
        *,
        future_only: bool = False,
        limit: int = 50,
    ) -> list[EventOccurrence]:
        where = "series_id = ?"
        values: list[object] = [series_id]
        if future_only:
            where += " AND actual_end_at > ? AND status IN (?, ?)"
            values.extend([_iso(datetime.now(UTC)), OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED])
        values.append(limit)
        with self.db._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM event_occurrences WHERE {where} ORDER BY actual_start_at LIMIT ?",
                values,
            ).fetchall()
        return [self._row_to_occurrence(row) for row in rows]

    def cancel_series(self, series_id: int, admin_id: int) -> EventSeries:
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE event_series SET status = ?, updated_at = ? WHERE id = ? AND status = ? AND created_by = ?",
                (SERIES_CANCELLED, now, series_id, SERIES_ACTIVE, admin_id),
            )
            if cursor.rowcount != 1:
                raise RequestNotEditableError("active series not found")
            connection.execute(
                """
                UPDATE event_occurrences SET status = ?, updated_at = ?
                WHERE series_id = ? AND status IN (?, ?)
                """,
                (OCCURRENCE_CANCELLED, now, series_id, OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED),
            )
            self._cancel_jobs_for_series(connection, series_id, now)
            self.db._audit(connection, admin_id, "series_cancelled", "event_series", str(series_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_series(series_id)
        if result is None:
            raise RuntimeError("cancelled series not found")
        return result

    def move_occurrence(
        self,
        occurrence_id: int,
        admin_id: int,
        start_at: datetime,
        end_at: datetime,
    ) -> EventOccurrence:
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            owned = connection.execute(
                """
                SELECT o.id FROM event_occurrences AS o
                JOIN event_series AS s ON s.id = o.series_id
                WHERE o.id = ? AND s.created_by = ? AND s.status = ?
                  AND o.status IN (?, ?)
                """,
                (occurrence_id, admin_id, SERIES_ACTIVE, OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED),
            ).fetchone()
            if owned is None:
                raise RequestNotEditableError("occurrence not found")
            cursor = connection.execute(
                """
                UPDATE event_occurrences
                SET actual_start_at = ?, actual_end_at = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (_iso(start_at), _iso(end_at), OCCURRENCE_MOVED, now, occurrence_id),
            )
            if cursor.rowcount != 1:
                raise RequestNotEditableError("occurrence not moved")
            self._cancel_jobs_for_occurrence(connection, occurrence_id, now)
            self.db._audit(connection, admin_id, "occurrence_moved", "event_occurrence", str(occurrence_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_occurrence(occurrence_id)
        if result is None:
            raise RuntimeError("moved occurrence not found")
        return result

    def cancel_occurrence(self, occurrence_id: int, admin_id: int) -> EventOccurrence:
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE event_occurrences SET status = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?) AND EXISTS (
                    SELECT 1 FROM event_series AS s
                    WHERE s.id = event_occurrences.series_id AND s.created_by = ? AND s.status = ?
                )
                """,
                (OCCURRENCE_CANCELLED, now, occurrence_id, OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED, admin_id, SERIES_ACTIVE),
            )
            if cursor.rowcount != 1:
                raise RequestNotEditableError("occurrence not found")
            self._cancel_jobs_for_occurrence(connection, occurrence_id, now)
            self.db._audit(connection, admin_id, "occurrence_cancelled", "event_occurrence", str(occurrence_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_occurrence(occurrence_id)
        if result is None:
            raise RuntimeError("cancelled occurrence not found")
        return result

    def retime_series(
        self,
        series_id: int,
        admin_id: int,
        start_at: datetime,
        end_at: datetime,
        occurrences: list[tuple[datetime, datetime]],
    ) -> EventSeries:
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            owned = connection.execute(
                "SELECT id FROM event_series WHERE id = ? AND created_by = ? AND status = ?",
                (series_id, admin_id, SERIES_ACTIVE),
            ).fetchone()
            rows = connection.execute(
                "SELECT id, status FROM event_occurrences WHERE series_id = ? ORDER BY expected_start_at",
                (series_id,),
            ).fetchall()
            if owned is None or len(rows) != len(occurrences):
                raise RequestNotEditableError("series cannot be retimed")
            if any(str(row["status"]) != OCCURRENCE_SCHEDULED for row in rows):
                raise RequestNotEditableError("series has occurrence exceptions")
            connection.execute(
                "UPDATE event_series SET start_at = ?, end_at = ?, updated_at = ? WHERE id = ?",
                (_iso(start_at), _iso(end_at), now, series_id),
            )
            for row, (item_start, item_end) in zip(rows, occurrences, strict=True):
                connection.execute(
                    """
                    UPDATE event_occurrences
                    SET expected_start_at = ?, expected_end_at = ?, actual_start_at = ?,
                        actual_end_at = ?, updated_at = ? WHERE id = ?
                    """,
                    (_iso(item_start), _iso(item_end), _iso(item_start), _iso(item_end), now, int(row["id"])),
                )
            self._cancel_jobs_for_series(connection, series_id, now)
            self.db._audit(connection, admin_id, "series_retimed", "event_series", str(series_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_series(series_id)
        if result is None:
            raise RuntimeError("retimed series not found")
        return result

    def rebuild_request_reminders(
        self,
        request_id: int,
        admin_id: int,
        reminder_minutes: Iterable[int],
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        request = self.db.get_request(request_id)
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._cancel_jobs_for_request(connection, request_id, _iso(current), JOB_MEETING_REMINDER)
            count = 0
            if request and request.status == APPROVED and request.start_at > current:
                recipients = {request.telegram_id, admin_id}
                for recipient in recipients:
                    for minutes in reminder_minutes:
                        due = request.start_at - timedelta(minutes=int(minutes))
                        if due < current:
                            due = current
                        key = f"meeting:{request.id}:{request.start_at.isoformat()}:{recipient}:{int(minutes)}"
                        count += self._insert_job(
                            connection,
                            JOB_MEETING_REMINDER,
                            request.id,
                            None,
                            recipient,
                            due,
                            key,
                            current,
                        )
            connection.commit()
            return count
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rebuild_occurrence_reminders(
        self,
        occurrence_id: int,
        recipient_id: int,
        reminder_minutes: Iterable[int],
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        occurrence = self.get_occurrence(occurrence_id)
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._cancel_jobs_for_occurrence(connection, occurrence_id, _iso(current))
            count = 0
            if occurrence and occurrence.status in {OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED} and occurrence.actual_start_at > current:
                for minutes in reminder_minutes:
                    due = occurrence.actual_start_at - timedelta(minutes=int(minutes))
                    if due < current:
                        due = current
                    key = f"occurrence:{occurrence.id}:{occurrence.actual_start_at.isoformat()}:{recipient_id}:{int(minutes)}"
                    count += self._insert_job(
                        connection,
                        JOB_MEETING_REMINDER,
                        None,
                        occurrence.id,
                        recipient_id,
                        due,
                        key,
                        current,
                    )
            connection.commit()
            return count
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_pending_reminder(
        self,
        request_id: int,
        admin_id: int,
        interval_hours: int,
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        request = self.db.get_request(request_id)
        if request is None or request.status != PENDING:
            return 0
        due = request.created_at + timedelta(hours=interval_hours)
        if due < current:
            due = current
        key = f"pending:{request.id}:{due.isoformat()}:{admin_id}"
        with self.db._connect() as connection:
            return self._insert_job(connection, JOB_PENDING_REMINDER, request.id, None, admin_id, due, key, current)

    def ensure_new_request_notification(
        self,
        request_id: int,
        admin_id: int,
        now: datetime | None = None,
    ) -> int:
        """Schedule one immediate, idempotent notification for a new request."""
        current = now or datetime.now(UTC)
        request = self.db.get_request(request_id)
        if request is None or request.status != PENDING:
            return 0
        key = f"new-request:{request.id}:{admin_id}"
        with self.db._connect() as connection:
            return self._insert_job(
                connection,
                JOB_NEW_REQUEST_NOTIFICATION,
                request.id,
                None,
                admin_id,
                current,
                key,
                current,
            )

    def schedule_next_pending_reminder(
        self,
        request_id: int,
        admin_id: int,
        interval_hours: int,
        after: datetime | None = None,
    ) -> int:
        current = after or datetime.now(UTC)
        request = self.db.get_request(request_id)
        if request is None or request.status != PENDING:
            return 0
        due = current + timedelta(hours=interval_hours)
        key = f"pending:{request.id}:{due.isoformat()}:{admin_id}"
        with self.db._connect() as connection:
            return self._insert_job(connection, JOB_PENDING_REMINDER, request.id, None, admin_id, due, key, current)

    def cancel_request_jobs(self, request_id: int) -> None:
        with self.db._connect() as connection:
            self._cancel_jobs_for_request(connection, request_id, _iso(datetime.now(UTC)))

    def bootstrap_jobs(
        self,
        admin_id: int,
        reminder_minutes: Iterable[int],
        pending_interval_hours: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        current = now or datetime.now(UTC)
        with self.db._connect() as connection:
            approved = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM meeting_requests WHERE status = ? AND start_at > ?",
                    (APPROVED, _iso(current)),
                ).fetchall()
            ]
            pending = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM meeting_requests WHERE status = ?",
                    (PENDING,),
                ).fetchall()
            ]
            occurrences = [
                (int(row["id"]), int(row["created_by"]))
                for row in connection.execute(
                    """
                    SELECT o.id, s.created_by FROM event_occurrences AS o
                    JOIN event_series AS s ON s.id = o.series_id
                    WHERE s.status = ? AND o.status IN (?, ?) AND o.actual_start_at > ?
                    """,
                    (SERIES_ACTIVE, OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED, _iso(current)),
                ).fetchall()
            ]
        request_jobs = sum(
            self.rebuild_request_reminders(item, admin_id, reminder_minutes, current)
            for item in approved
        )
        pending_jobs = sum(
            self.ensure_pending_reminder(item, admin_id, pending_interval_hours, current)
            for item in pending
        )
        occurrence_jobs = sum(
            self.rebuild_occurrence_reminders(item, owner, reminder_minutes, current)
            for item, owner in occurrences
        )
        return {
            "requests": len(approved),
            "pending": len(pending),
            "occurrences": len(occurrences),
            "jobs": request_jobs + pending_jobs + occurrence_jobs,
        }

    def claim_due_jobs(self, now: datetime | None = None, limit: int = 20) -> list[ScheduledJob]:
        current = now or datetime.now(UTC)
        now_text = _iso(current)
        stale = _iso(current - timedelta(minutes=5))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE scheduled_jobs SET status = ?, claimed_at = NULL, updated_at = ?
                WHERE status = ? AND claimed_at < ? AND attempt_count < max_attempts
                """,
                (JOB_PENDING, now_text, JOB_PROCESSING, stale),
            )
            rows = connection.execute(
                """
                SELECT id FROM scheduled_jobs
                WHERE status = ? AND due_at <= ? AND attempt_count < max_attempts
                ORDER BY due_at, id LIMIT ?
                """,
                (JOB_PENDING, now_text, limit),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            for job_id in ids:
                connection.execute(
                    """
                    UPDATE scheduled_jobs
                    SET status = ?, attempt_count = attempt_count + 1, claimed_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (JOB_PROCESSING, now_text, now_text, job_id, JOB_PENDING),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.db._connect() as connection:
            claimed = connection.execute(
                f"SELECT * FROM scheduled_jobs WHERE id IN ({placeholders}) AND status = ? ORDER BY due_at, id",
                (*ids, JOB_PROCESSING),
            ).fetchall()
        return [self._row_to_job(row) for row in claimed]

    def complete_job(self, job_id: int, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        now_text = _iso(current)
        with self.db._connect() as connection:
            row = connection.execute("SELECT attempt_count FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE scheduled_jobs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (JOB_DONE, now_text, job_id, JOB_PROCESSING),
            )
            connection.execute(
                """
                INSERT INTO notification_deliveries (job_id, attempt_number, attempted_at, result)
                VALUES (?, ?, ?, 'DELIVERED')
                """,
                (job_id, int(row["attempt_count"]), now_text),
            )

    def fail_job(self, job_id: int, error_code: str, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        now_text = _iso(current)
        with self.db._connect() as connection:
            row = connection.execute(
                "SELECT attempt_count, max_attempts FROM scheduled_jobs WHERE id = ? AND status = ?",
                (job_id, JOB_PROCESSING),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempt_count"])
            failed = attempts >= int(row["max_attempts"])
            retry_minutes = (1, 5, 15, 60, 180)[min(attempts - 1, 4)]
            connection.execute(
                """
                UPDATE scheduled_jobs
                SET status = ?, due_at = ?, claimed_at = NULL, last_error_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (JOB_FAILED if failed else JOB_PENDING, _iso(current + timedelta(minutes=retry_minutes)), error_code, now_text, job_id),
            )
            connection.execute(
                """
                INSERT INTO notification_deliveries (job_id, attempt_number, attempted_at, result, error_code)
                VALUES (?, ?, ?, 'FAILED', ?)
                """,
                (job_id, attempts, now_text, error_code),
            )

    def get_job_subject(self, job: ScheduledJob) -> tuple[MeetingRequest | None, EventOccurrence | None, EventSeries | None]:
        request = self.db.get_request(job.request_id) if job.request_id else None
        occurrence = self.get_occurrence(job.occurrence_id) if job.occurrence_id else None
        series = self.get_series(occurrence.series_id) if occurrence else None
        return request, occurrence, series

    def list_sync_candidates(self, limit: int = 20) -> list[MeetingRequest]:
        with self.db._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM meeting_requests
                WHERE status = ? AND google_event_id IS NOT NULL AND end_at > ?
                ORDER BY COALESCE(last_synced_at, '1970-01-01'), start_at LIMIT ?
                """,
                (APPROVED, _iso(datetime.now(UTC)), limit),
            ).fetchall()
        return [self.db._row_to_request(row) for row in rows]

    def list_occurrence_sync_candidates(
        self,
        limit: int = 20,
    ) -> list[tuple[EventOccurrence, EventSeries]]:
        with self.db._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.id AS occurrence_id, o.series_id
                FROM event_occurrences AS o
                JOIN event_series AS s ON s.id = o.series_id
                WHERE s.status = ? AND s.google_series_id IS NOT NULL
                  AND o.status IN (?, ?) AND o.expected_end_at > ?
                ORDER BY COALESCE(o.last_synced_at, '1970-01-01'), o.expected_start_at
                LIMIT ?
                """,
                (SERIES_ACTIVE, OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED, _iso(datetime.now(UTC)), limit),
            ).fetchall()
        result: list[tuple[EventOccurrence, EventSeries]] = []
        for row in rows:
            occurrence = self.get_occurrence(int(row["occurrence_id"]))
            series = self.get_series(int(row["series_id"]))
            if occurrence and series:
                result.append((occurrence, series))
        return result

    def apply_occurrence_state(
        self,
        occurrence_id: int,
        state: CalendarEventState,
    ) -> tuple[EventOccurrence, EventSeries, bool]:
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT o.* FROM event_occurrences AS o
                JOIN event_series AS s ON s.id = o.series_id
                WHERE o.id = ? AND s.status = ? AND o.status IN (?, ?)
                """,
                (occurrence_id, SERIES_ACTIVE, OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED),
            ).fetchone()
            if row is None:
                raise RequestNotEditableError("active occurrence not found")
            changed = False
            if not state.exists:
                connection.execute(
                    """
                    UPDATE event_occurrences
                    SET status = ?, sync_state = ?, last_synced_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (OCCURRENCE_MISSING, SYNC_MISSING, now, now, occurrence_id),
                )
                self._cancel_jobs_for_occurrence(connection, occurrence_id, now)
                changed = True
            else:
                old_start = _dt(str(row["actual_start_at"]))
                old_end = _dt(str(row["actual_end_at"]))
                new_start = state.start_at or old_start
                new_end = state.end_at or old_end
                changed = old_start != new_start or old_end != new_end
                expected_start = _dt(str(row["expected_start_at"]))
                expected_end = _dt(str(row["expected_end_at"]))
                status = (
                    OCCURRENCE_SCHEDULED
                    if new_start == expected_start and new_end == expected_end
                    else OCCURRENCE_MOVED
                )
                connection.execute(
                    """
                    UPDATE event_occurrences
                    SET actual_start_at = ?, actual_end_at = ?, status = ?, google_event_id = ?,
                        sync_state = ?, last_synced_at = ?, google_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _iso(new_start),
                        _iso(new_end),
                        status,
                        state.event_id,
                        SYNC_CHANGED if changed else SYNCED,
                        now,
                        _iso(state.updated_at) if state.updated_at else None,
                        now,
                        occurrence_id,
                    ),
                )
                if changed:
                    self._cancel_jobs_for_occurrence(connection, occurrence_id, now)
            connection.execute(
                "UPDATE event_series SET last_synced_at = ?, sync_state = ?, updated_at = ? WHERE id = ?",
                (now, SYNC_CHANGED if changed else SYNCED, now, int(row["series_id"])),
            )
            self.db._audit(
                connection,
                None,
                "google_occurrence_synced",
                "event_occurrence",
                str(occurrence_id),
                {"changed": changed, "exists": state.exists},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        occurrence = self.get_occurrence(occurrence_id)
        series = self.get_series(occurrence.series_id) if occurrence else None
        if occurrence is None or series is None:
            raise RuntimeError("synced occurrence not found")
        return occurrence, series, changed

    def apply_event_state(self, request_id: int, state: CalendarEventState) -> tuple[MeetingRequest, bool]:
        now = _iso(datetime.now(UTC))
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM meeting_requests WHERE id = ? AND status = ?", (request_id, APPROVED)).fetchone()
            if row is None:
                raise RequestNotEditableError("approved request not found")
            changed = False
            if not state.exists:
                connection.execute(
                    """
                    UPDATE meeting_requests SET status = ?, sync_state = ?, last_synced_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (CANCELLED_BY_ADMIN, SYNC_MISSING, now, now, request_id),
                )
                self._cancel_jobs_for_request(connection, request_id, now)
                changed = True
            else:
                updates: dict[str, object] = {}
                if state.start_at and _dt(str(row["start_at"])) != state.start_at.astimezone(UTC):
                    updates["start_at"] = _iso(state.start_at)
                if state.end_at and _dt(str(row["end_at"])) != state.end_at.astimezone(UTC):
                    updates["end_at"] = _iso(state.end_at)
                if state.subject is not None and str(row["subject"]) != state.subject:
                    updates["subject"] = state.subject
                if state.location is not None and str(row["location"] or "") != state.location:
                    updates["location"] = state.location or None
                if state.blocks_calendar is not None and bool(row["blocks_calendar"]) != state.blocks_calendar:
                    updates["blocks_calendar"] = int(state.blocks_calendar)
                changed = bool(updates)
                assignments = ", ".join(f"{key} = ?" for key in updates)
                values = list(updates.values())
                if assignments:
                    assignments += ", "
                connection.execute(
                    f"""
                    UPDATE meeting_requests
                    SET {assignments}sync_state = ?, last_synced_at = ?, google_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (*values, SYNC_CHANGED if changed else SYNCED, now, _iso(state.updated_at) if state.updated_at else None, now, request_id),
                )
            self.db._audit(connection, None, "google_event_synced", "meeting_request", str(request_id), {"changed": changed, "exists": state.exists})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        request = self.db.get_request(request_id)
        if request is None:
            raise RuntimeError("synced request not found")
        return request, changed

    def start_sync_run(self) -> int:
        now = _iso(datetime.now(UTC))
        with self.db._connect() as connection:
            cursor = connection.execute("INSERT INTO sync_runs (started_at, status) VALUES (?, 'RUNNING')", (now,))
            return int(cursor.lastrowid)

    def finish_sync_run(self, run_id: int, checked: int, changed: int, missing: int, errors: int) -> None:
        with self.db._connect() as connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET finished_at = ?, checked_count = ?, changed_count = ?, missing_count = ?, error_count = ?, status = ?
                WHERE id = ?
                """,
                (_iso(datetime.now(UTC)), checked, changed, missing, errors, "FAILED" if errors and not checked else "DONE", run_id),
            )

    def _slot_conflict(self, connection: sqlite3.Connection, start_at: datetime, end_at: datetime) -> bool:
        now = _iso(datetime.now(UTC))
        request = connection.execute(
            """
            SELECT 1 FROM meeting_requests
            WHERE blocks_calendar = 1 AND start_at < ? AND end_at > ?
              AND (status = ? OR (status IN (?, ?) AND hold_until > ?)) LIMIT 1
            """,
            (_iso(end_at), _iso(start_at), APPROVED, PENDING, APPROVING, now),
        ).fetchone()
        alternative = connection.execute(
            """
            SELECT 1 FROM request_alternatives AS a
            JOIN meeting_requests AS r ON r.id = a.request_id
            WHERE r.blocks_calendar = 1 AND a.status = ? AND a.hold_until > ?
              AND a.start_at < ? AND a.end_at > ? LIMIT 1
            """,
            (ALTERNATIVE_OFFERED, now, _iso(end_at), _iso(start_at)),
        ).fetchone()
        change = connection.execute(
            """
            SELECT 1 FROM change_requests AS c
            JOIN meeting_requests AS r ON r.id = c.request_id
            WHERE r.blocks_calendar = 1 AND c.change_type = ? AND c.status IN (?, ?)
              AND c.proposed_start_at < ? AND c.proposed_end_at > ? LIMIT 1
            """,
            (CHANGE_RESCHEDULE, CHANGE_PENDING, CHANGE_APPROVING, _iso(end_at), _iso(start_at)),
        ).fetchone()
        occurrence = connection.execute(
            """
            SELECT 1 FROM event_occurrences AS o
            JOIN event_series AS s ON s.id = o.series_id
            WHERE s.blocks_calendar = 1 AND s.status IN (?, ?)
              AND o.status IN (?, ?) AND o.actual_start_at < ? AND o.actual_end_at > ? LIMIT 1
            """,
            (SERIES_ACTIVE, SERIES_CREATING, OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED, _iso(end_at), _iso(start_at)),
        ).fetchone()
        return bool(request or alternative or change or occurrence)

    def _insert_job(
        self,
        connection: sqlite3.Connection,
        job_type: str,
        request_id: int | None,
        occurrence_id: int | None,
        recipient_id: int,
        due_at: datetime,
        key: str,
        now: datetime,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO scheduled_jobs (
                job_type, request_id, occurrence_id, recipient_telegram_id, due_at,
                status, idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                due_at = excluded.due_at,
                status = CASE
                    WHEN scheduled_jobs.status = 'CANCELLED' THEN 'PENDING'
                    ELSE scheduled_jobs.status
                END,
                updated_at = excluded.updated_at
            """,
            (job_type, request_id, occurrence_id, recipient_id, _iso(due_at), JOB_PENDING, key, _iso(now), _iso(now)),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _cancel_jobs_for_request(connection: sqlite3.Connection, request_id: int, now: str, job_type: str | None = None) -> None:
        condition = "request_id = ? AND status IN (?, ?)"
        values: list[object] = [request_id, JOB_PENDING, JOB_PROCESSING]
        if job_type:
            condition += " AND job_type = ?"
            values.append(job_type)
        connection.execute(f"UPDATE scheduled_jobs SET status = ?, updated_at = ? WHERE {condition}", (JOB_CANCELLED, now, *values))

    @staticmethod
    def _cancel_jobs_for_occurrence(connection: sqlite3.Connection, occurrence_id: int, now: str) -> None:
        connection.execute(
            """
            UPDATE scheduled_jobs SET status = ?, updated_at = ?
            WHERE occurrence_id = ? AND status IN (?, ?)
            """,
            (JOB_CANCELLED, now, occurrence_id, JOB_PENDING, JOB_PROCESSING),
        )

    @staticmethod
    def _cancel_jobs_for_series(connection: sqlite3.Connection, series_id: int, now: str) -> None:
        connection.execute(
            """
            UPDATE scheduled_jobs SET status = ?, updated_at = ?
            WHERE occurrence_id IN (SELECT id FROM event_occurrences WHERE series_id = ?)
              AND status IN (?, ?)
            """,
            (JOB_CANCELLED, now, series_id, JOB_PENDING, JOB_PROCESSING),
        )

    @staticmethod
    def _row_to_series(row: sqlite3.Row) -> EventSeries:
        return EventSeries(
            id=int(row["id"]),
            created_by=int(row["created_by"]),
            admin_name=str(row["admin_name"]),
            admin_username=row["admin_username"],
            email=row["email"],
            subject=str(row["subject"]),
            description=row["description"],
            location=row["location"],
            start_at=_dt(str(row["start_at"])),
            end_at=_dt(str(row["end_at"])),
            frequency=str(row["frequency"]),
            until_date=str(row["until_date"]),
            status=str(row["status"]),
            google_series_id=row["google_series_id"],
            blocks_calendar=bool(row["blocks_calendar"]),
            allow_overlap=bool(row["allow_overlap"]),
            sync_state=str(row["sync_state"]),
            last_synced_at=_dt(str(row["last_synced_at"])) if row["last_synced_at"] else None,
            created_at=_dt(str(row["created_at"])),
            updated_at=_dt(str(row["updated_at"])),
        )

    @staticmethod
    def _row_to_occurrence(row: sqlite3.Row) -> EventOccurrence:
        return EventOccurrence(
            id=int(row["id"]),
            series_id=int(row["series_id"]),
            expected_start_at=_dt(str(row["expected_start_at"])),
            expected_end_at=_dt(str(row["expected_end_at"])),
            actual_start_at=_dt(str(row["actual_start_at"])),
            actual_end_at=_dt(str(row["actual_end_at"])),
            status=str(row["status"]),
            google_event_id=row["google_event_id"],
            created_at=_dt(str(row["created_at"])),
            updated_at=_dt(str(row["updated_at"])),
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> ScheduledJob:
        return ScheduledJob(
            id=int(row["id"]),
            job_type=str(row["job_type"]),
            request_id=int(row["request_id"]) if row["request_id"] is not None else None,
            occurrence_id=int(row["occurrence_id"]) if row["occurrence_id"] is not None else None,
            recipient_telegram_id=int(row["recipient_telegram_id"]),
            due_at=_dt(str(row["due_at"])),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            idempotency_key=str(row["idempotency_key"]),
            claimed_at=_dt(str(row["claimed_at"])) if row["claimed_at"] else None,
            last_error_code=row["last_error_code"],
            created_at=_dt(str(row["created_at"])),
            updated_at=_dt(str(row["updated_at"])),
        )

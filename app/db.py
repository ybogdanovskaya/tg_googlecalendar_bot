from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.migrations import MigrationResult, migrate_database
from app.models import (
    ALTERNATIVE_ACCEPTED,
    ALTERNATIVE_DECLINED,
    ALTERNATIVE_OFFERED,
    ALTERNATIVE_WITHDRAWN,
    APPROVED,
    APPROVING,
    CANCELLED,
    CANCELLED_BY_ADMIN,
    CHANGE_APPROVED,
    CHANGE_APPROVING,
    CHANGE_CANCEL,
    CHANGE_FAILED,
    CHANGE_PENDING,
    CHANGE_REJECTED,
    CHANGE_RESCHEDULE,
    PENDING,
    REJECTED,
    ChangeRequest,
    MeetingRequest,
    RequestAlternative,
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class SlotConflictError(RuntimeError):
    pass


class RequestNotEditableError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path, backup_dir: Path | None = None) -> None:
        self.path = path
        self.backup_dir = backup_dir
        self.last_migration_result: MigrationResult | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        self.last_migration_result = migrate_database(self.path, self.backup_dir)

    def upsert_user(self, telegram_id: int, name: str, username: str | None) -> None:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    telegram_id, telegram_name, telegram_username, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    telegram_name = excluded.telegram_name,
                    telegram_username = excluded.telegram_username,
                    updated_at = excluded.updated_at
                """,
                (telegram_id, name, username, now, now),
            )

    def set_consent(self, telegram_id: int, version: str) -> None:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE users
                SET consent_version = ?, consent_at = ?, updated_at = ?
                WHERE telegram_id = ?
                """,
                (version, now, now, telegram_id),
            )
            self._audit(connection, telegram_id, "consent_accepted", "user", str(telegram_id), {"version": version})

    def has_consent(self, telegram_id: int, version: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT consent_version FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return bool(row and row["consent_version"] == version)

    def active_intervals(
        self,
        range_start: datetime,
        range_end: datetime,
        exclude_request_id: int | None = None,
    ) -> list[tuple[datetime, datetime]]:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT start_at, end_at
                FROM meeting_requests
                WHERE start_at < ? AND end_at > ?
                  AND (
                    status = ?
                    OR (status IN (?, ?) AND hold_until > ?)
                  )
                  AND (? IS NULL OR id <> ?)
                """,
                (
                    _iso(range_end),
                    _iso(range_start),
                    APPROVED,
                    PENDING,
                    APPROVING,
                    now,
                    exclude_request_id,
                    exclude_request_id,
                ),
            ).fetchall()
            alternatives = connection.execute(
                """
                SELECT a.start_at, a.end_at
                FROM request_alternatives AS a
                JOIN meeting_requests AS r ON r.id = a.request_id
                WHERE a.start_at < ? AND a.end_at > ?
                  AND a.status = ? AND a.hold_until > ?
                  AND (? IS NULL OR r.id <> ?)
                """,
                (
                    _iso(range_end),
                    _iso(range_start),
                    ALTERNATIVE_OFFERED,
                    now,
                    exclude_request_id,
                    exclude_request_id,
                ),
            ).fetchall()
            changes = connection.execute(
                """
                SELECT c.proposed_start_at AS start_at, c.proposed_end_at AS end_at
                FROM change_requests AS c
                WHERE c.change_type = ?
                  AND c.status IN (?, ?)
                  AND c.proposed_start_at < ? AND c.proposed_end_at > ?
                  AND (? IS NULL OR c.request_id <> ?)
                """,
                (
                    CHANGE_RESCHEDULE,
                    CHANGE_PENDING,
                    CHANGE_APPROVING,
                    _iso(range_end),
                    _iso(range_start),
                    exclude_request_id,
                    exclude_request_id,
                ),
            ).fetchall()
        return [
            (_dt(row["start_at"]), _dt(row["end_at"]))
            for row in [*rows, *alternatives, *changes]
        ]

    def create_request(
        self,
        *,
        telegram_id: int,
        telegram_name: str,
        telegram_username: str | None,
        email: str,
        subject: str,
        description: str | None,
        location: str | None,
        start_at: datetime,
        end_at: datetime,
        hold_hours: int,
    ) -> MeetingRequest:
        now_dt = datetime.now(UTC)
        now = _iso(now_dt)
        hold_until = _iso(now_dt + timedelta(hours=hold_hours))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            conflict = connection.execute(
                """
                SELECT id FROM meeting_requests
                WHERE start_at < ? AND end_at > ?
                  AND (
                    status = ?
                    OR (status IN (?, ?) AND hold_until > ?)
                  )
                LIMIT 1
                """,
                (_iso(end_at), _iso(start_at), APPROVED, PENDING, APPROVING, now),
            ).fetchone()
            if conflict:
                raise SlotConflictError("slot already reserved")
            cursor = connection.execute(
                """
                INSERT INTO meeting_requests (
                    telegram_id, telegram_name, telegram_username, email, subject,
                    description, location, start_at, end_at, status, hold_until,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    telegram_name,
                    telegram_username,
                    email,
                    subject,
                    description,
                    location,
                    _iso(start_at),
                    _iso(end_at),
                    PENDING,
                    hold_until,
                    now,
                    now,
                ),
            )
            request_id = int(cursor.lastrowid)
            self._audit(connection, telegram_id, "request_created", "meeting_request", str(request_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        request = self.get_request(request_id)
        if request is None:
            raise RuntimeError("created request not found")
        return request

    def get_request(self, request_id: int) -> MeetingRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM meeting_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        return self._row_to_request(row) if row else None

    def list_user_requests(self, telegram_id: int, limit: int = 10) -> list[MeetingRequest]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM meeting_requests
                    WHERE telegram_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                ) AS recent_requests
                ORDER BY created_at ASC, id ASC
                """,
                (telegram_id, limit),
            ).fetchall()
        return [self._row_to_request(row) for row in rows]

    def list_pending(self, limit: int = 20) -> list[MeetingRequest]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM meeting_requests
                WHERE status IN (?, ?)
                ORDER BY start_at
                LIMIT ?
                """,
                (PENDING, APPROVING, limit),
            ).fetchall()
        return [self._row_to_request(row) for row in rows]

    def update_pending_details(
        self,
        request_id: int,
        telegram_id: int,
        changes: dict[str, str | None],
    ) -> MeetingRequest:
        allowed = {
            "telegram_name": "telegram_name",
            "email": "email",
            "subject": "subject",
            "description": "description",
            "location": "location",
        }
        normalized = {allowed[key]: value for key, value in changes.items() if key in allowed}
        if not normalized or len(normalized) != len(changes):
            raise ValueError("no editable fields supplied")
        now = _iso(datetime.now(UTC))
        assignments = ", ".join(f"{column} = ?" for column in normalized)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE meeting_requests
                SET {assignments}, updated_at = ?
                WHERE id = ? AND telegram_id = ? AND status = ?
                """,
                (*normalized.values(), now, request_id, telegram_id, PENDING),
            )
            if cursor.rowcount != 1:
                raise RequestNotEditableError("request is not editable")
            self._audit(
                connection,
                telegram_id,
                "request_details_updated",
                "meeting_request",
                str(request_id),
                {"fields": sorted(normalized)},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        request = self.get_request(request_id)
        if request is None:
            raise RuntimeError("updated request not found")
        return request

    def reschedule_pending(
        self,
        request_id: int,
        telegram_id: int,
        start_at: datetime,
        end_at: datetime,
        hold_hours: int,
    ) -> MeetingRequest:
        now_dt = datetime.now(UTC)
        now = _iso(now_dt)
        hold_until = _iso(now_dt + timedelta(hours=hold_hours))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            owned = connection.execute(
                "SELECT id FROM meeting_requests WHERE id = ? AND telegram_id = ? AND status = ?",
                (request_id, telegram_id, PENDING),
            ).fetchone()
            if owned is None:
                raise RequestNotEditableError("request is not editable")
            conflict = connection.execute(
                """
                SELECT id FROM meeting_requests
                WHERE id <> ? AND start_at < ? AND end_at > ?
                  AND (
                    status = ?
                    OR (status IN (?, ?) AND hold_until > ?)
                  )
                LIMIT 1
                """,
                (
                    request_id,
                    _iso(end_at),
                    _iso(start_at),
                    APPROVED,
                    PENDING,
                    APPROVING,
                    now,
                ),
            ).fetchone()
            if conflict:
                raise SlotConflictError("slot already reserved")
            connection.execute(
                """
                UPDATE meeting_requests
                SET start_at = ?, end_at = ?, hold_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (_iso(start_at), _iso(end_at), hold_until, now, request_id),
            )
            self._audit(
                connection,
                telegram_id,
                "request_rescheduled",
                "meeting_request",
                str(request_id),
                {"hold_hours": hold_hours},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        request = self.get_request(request_id)
        if request is None:
            raise RuntimeError("rescheduled request not found")
        return request

    def admin_update_pending_details(
        self,
        request_id: int,
        admin_id: int,
        changes: dict[str, str | None],
    ) -> MeetingRequest:
        allowed = {"telegram_name", "email", "subject", "description", "location"}
        if not changes or any(key not in allowed for key in changes):
            raise ValueError("no editable fields supplied")
        now = _iso(datetime.now(UTC))
        assignments = ", ".join(f"{key} = ?" for key in changes)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE meeting_requests SET {assignments}, updated_at = ? WHERE id = ? AND status = ?",
                (*changes.values(), now, request_id, PENDING),
            )
            if cursor.rowcount != 1:
                raise RequestNotEditableError("request is not editable")
            self._audit(
                connection,
                admin_id,
                "request_admin_updated",
                "meeting_request",
                str(request_id),
                {"fields": sorted(changes)},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        request = self.get_request(request_id)
        if request is None:
            raise RuntimeError("updated request not found")
        return request

    def create_alternative(
        self,
        request_id: int,
        admin_id: int,
        start_at: datetime,
        end_at: datetime,
        hold_hours: int,
    ) -> RequestAlternative:
        now_dt = datetime.now(UTC)
        now = _iso(now_dt)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT id FROM meeting_requests WHERE id = ? AND status = ?",
                (request_id, PENDING),
            ).fetchone()
            if request is None:
                raise RequestNotEditableError("request is not pending")
            count = connection.execute(
                "SELECT COUNT(*) FROM request_alternatives WHERE request_id = ? AND status = ?",
                (request_id, ALTERNATIVE_OFFERED),
            ).fetchone()[0]
            if int(count) >= 3:
                raise ValueError("maximum alternatives reached")
            conflict = connection.execute(
                """
                SELECT id FROM meeting_requests
                WHERE id <> ? AND start_at < ? AND end_at > ?
                  AND (status = ? OR (status IN (?, ?) AND hold_until > ?))
                LIMIT 1
                """,
                (request_id, _iso(end_at), _iso(start_at), APPROVED, PENDING, APPROVING, now),
            ).fetchone()
            alt_conflict = connection.execute(
                """
                SELECT id FROM request_alternatives
                WHERE start_at < ? AND end_at > ? AND status = ? AND hold_until > ?
                LIMIT 1
                """,
                (_iso(end_at), _iso(start_at), ALTERNATIVE_OFFERED, now),
            ).fetchone()
            if conflict or alt_conflict:
                raise SlotConflictError("alternative slot is reserved")
            cursor = connection.execute(
                """
                INSERT INTO request_alternatives (
                    request_id, start_at, end_at, status, hold_until, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    _iso(start_at),
                    _iso(end_at),
                    ALTERNATIVE_OFFERED,
                    _iso(now_dt + timedelta(hours=hold_hours)),
                    admin_id,
                    now,
                ),
            )
            alternative_id = int(cursor.lastrowid)
            self._audit(connection, admin_id, "alternative_created", "request_alternative", str(alternative_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        alternative = self.get_alternative(alternative_id)
        if alternative is None:
            raise RuntimeError("alternative not found")
        return alternative

    def get_alternative(self, alternative_id: int) -> RequestAlternative | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM request_alternatives WHERE id = ?",
                (alternative_id,),
            ).fetchone()
        return self._row_to_alternative(row) if row else None

    def list_offered_alternatives(self, request_id: int) -> list[RequestAlternative]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM request_alternatives
                WHERE request_id = ? AND status = ?
                ORDER BY start_at
                """,
                (request_id, ALTERNATIVE_OFFERED),
            ).fetchall()
        return [self._row_to_alternative(row) for row in rows]

    def accept_alternative(self, alternative_id: int, telegram_id: int, hold_hours: int) -> MeetingRequest:
        now_dt = datetime.now(UTC)
        now = _iso(now_dt)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT a.* FROM request_alternatives AS a
                JOIN meeting_requests AS r ON r.id = a.request_id
                WHERE a.id = ? AND r.telegram_id = ? AND r.status = ?
                  AND a.status = ? AND a.hold_until > ?
                """,
                (alternative_id, telegram_id, PENDING, ALTERNATIVE_OFFERED, now),
            ).fetchone()
            if row is None:
                raise RequestNotEditableError("alternative is unavailable")
            request_id = int(row["request_id"])
            conflict = connection.execute(
                """
                SELECT id FROM meeting_requests
                WHERE id <> ? AND start_at < ? AND end_at > ?
                  AND (status = ? OR (status IN (?, ?) AND hold_until > ?))
                LIMIT 1
                """,
                (request_id, row["end_at"], row["start_at"], APPROVED, PENDING, APPROVING, now),
            ).fetchone()
            if conflict:
                raise SlotConflictError("alternative slot is no longer free")
            connection.execute(
                """
                UPDATE meeting_requests
                SET start_at = ?, end_at = ?, hold_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    row["start_at"],
                    row["end_at"],
                    _iso(now_dt + timedelta(hours=hold_hours)),
                    now,
                    request_id,
                ),
            )
            connection.execute(
                "UPDATE request_alternatives SET status = ?, responded_at = ? WHERE id = ?",
                (ALTERNATIVE_ACCEPTED, now, alternative_id),
            )
            connection.execute(
                """
                UPDATE request_alternatives SET status = ?, responded_at = ?
                WHERE request_id = ? AND id <> ? AND status = ?
                """,
                (ALTERNATIVE_WITHDRAWN, now, request_id, alternative_id, ALTERNATIVE_OFFERED),
            )
            self._audit(connection, telegram_id, "alternative_accepted", "request_alternative", str(alternative_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        request = self.get_request(request_id)
        if request is None:
            raise RuntimeError("request not found")
        return request

    def decline_alternatives(self, request_id: int, telegram_id: int) -> int:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            owned = connection.execute(
                "SELECT 1 FROM meeting_requests WHERE id = ? AND telegram_id = ? AND status = ?",
                (request_id, telegram_id, PENDING),
            ).fetchone()
            if owned is None:
                raise RequestNotEditableError("request is unavailable")
            cursor = connection.execute(
                """
                UPDATE request_alternatives SET status = ?, responded_at = ?
                WHERE request_id = ? AND status = ?
                """,
                (ALTERNATIVE_DECLINED, now, request_id, ALTERNATIVE_OFFERED),
            )
            self._audit(connection, telegram_id, "alternatives_declined", "meeting_request", str(request_id), {})
            return cursor.rowcount

    def create_change_request(
        self,
        request_id: int,
        telegram_id: int,
        change_type: str,
        proposed_start_at: datetime | None = None,
        proposed_end_at: datetime | None = None,
    ) -> ChangeRequest:
        if change_type not in {CHANGE_CANCEL, CHANGE_RESCHEDULE}:
            raise ValueError("invalid change type")
        if change_type == CHANGE_RESCHEDULE and (proposed_start_at is None or proposed_end_at is None):
            raise ValueError("reschedule requires a range")
        now = _iso(datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            owned = connection.execute(
                "SELECT id FROM meeting_requests WHERE id = ? AND telegram_id = ? AND status = ?",
                (request_id, telegram_id, APPROVED),
            ).fetchone()
            if owned is None:
                raise RequestNotEditableError("approved meeting not found")
            if change_type == CHANGE_RESCHEDULE:
                conflict = connection.execute(
                    """
                    SELECT id FROM meeting_requests
                    WHERE id <> ? AND start_at < ? AND end_at > ?
                      AND status = ? LIMIT 1
                    """,
                    (request_id, _iso(proposed_end_at), _iso(proposed_start_at), APPROVED),
                ).fetchone()
                if conflict:
                    raise SlotConflictError("new slot is busy")
            cursor = connection.execute(
                """
                INSERT INTO change_requests (
                    request_id, change_type, proposed_start_at, proposed_end_at,
                    status, requested_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    change_type,
                    _iso(proposed_start_at) if proposed_start_at else None,
                    _iso(proposed_end_at) if proposed_end_at else None,
                    CHANGE_PENDING,
                    telegram_id,
                    now,
                    now,
                ),
            )
            change_id = int(cursor.lastrowid)
            self._audit(connection, telegram_id, "change_requested", "change_request", str(change_id), {"type": change_type})
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise RequestNotEditableError("an open change request already exists") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        change = self.get_change_request(change_id)
        if change is None:
            raise RuntimeError("change request not found")
        return change

    def get_change_request(self, change_id: int) -> ChangeRequest | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM change_requests WHERE id = ?", (change_id,)).fetchone()
        return self._row_to_change(row) if row else None

    def list_pending_changes(self, limit: int = 20) -> list[ChangeRequest]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM change_requests WHERE status IN (?, ?) ORDER BY created_at LIMIT ?",
                (CHANGE_PENDING, CHANGE_APPROVING, limit),
            ).fetchall()
        return [self._row_to_change(row) for row in rows]

    def claim_change(self, change_id: int, admin_id: int) -> ChangeRequest | None:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE change_requests SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (CHANGE_APPROVING, now, change_id, CHANGE_PENDING),
            )
            if cursor.rowcount != 1:
                return None
            self._audit(connection, admin_id, "change_claimed", "change_request", str(change_id), {})
        return self.get_change_request(change_id)

    def reset_change(self, change_id: int, reason: str) -> None:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                "UPDATE change_requests SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (CHANGE_PENDING, now, change_id, CHANGE_APPROVING),
            )
            self._audit(connection, None, "change_reset", "change_request", str(change_id), {"reason": reason})

    def reject_change(self, change_id: int, admin_id: int) -> ChangeRequest | None:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE change_requests SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (CHANGE_REJECTED, now, change_id, CHANGE_PENDING),
            )
            if cursor.rowcount != 1:
                return None
            self._audit(connection, admin_id, "change_rejected", "change_request", str(change_id), {})
        return self.get_change_request(change_id)

    def complete_change(self, change_id: int, admin_id: int) -> tuple[ChangeRequest, MeetingRequest]:
        now = _iso(datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM change_requests WHERE id = ? AND status = ?",
                (change_id, CHANGE_APPROVING),
            ).fetchone()
            if row is None:
                raise RequestNotEditableError("change is not being processed")
            request_id = int(row["request_id"])
            if row["change_type"] == CHANGE_CANCEL:
                connection.execute(
                    "UPDATE meeting_requests SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (CANCELLED_BY_ADMIN, now, request_id, APPROVED),
                )
            else:
                connection.execute(
                    """
                    UPDATE meeting_requests SET start_at = ?, end_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (row["proposed_start_at"], row["proposed_end_at"], now, request_id, APPROVED),
                )
            connection.execute(
                "UPDATE change_requests SET status = ?, updated_at = ? WHERE id = ?",
                (CHANGE_APPROVED, now, change_id),
            )
            self._audit(connection, admin_id, "change_completed", "change_request", str(change_id), {})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        change = self.get_change_request(change_id)
        request = self.get_request(request_id)
        if change is None or request is None:
            raise RuntimeError("completed change not found")
        return change, request

    def create_admin_draft(
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
        blocks_calendar: bool,
        allow_overlap: bool,
    ) -> MeetingRequest:
        now = _iso(datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not allow_overlap:
                conflict = connection.execute(
                    """
                    SELECT id FROM meeting_requests
                    WHERE start_at < ? AND end_at > ?
                      AND (status = ? OR (status IN (?, ?) AND hold_until > ?))
                    LIMIT 1
                    """,
                    (_iso(end_at), _iso(start_at), APPROVED, PENDING, APPROVING, now),
                ).fetchone()
                if conflict:
                    raise SlotConflictError("manual meeting overlaps")
            cursor = connection.execute(
                """
                INSERT INTO meeting_requests (
                    telegram_id, telegram_name, telegram_username, email, subject,
                    description, location, start_at, end_at, status, hold_until,
                    created_at, updated_at, source, blocks_calendar, admin_override
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ADMIN', ?, ?)
                """,
                (
                    admin_id,
                    admin_name,
                    admin_username,
                    email or "",
                    subject,
                    description,
                    location,
                    _iso(start_at),
                    _iso(end_at),
                    APPROVING,
                    now,
                    now,
                    now,
                    int(blocks_calendar),
                    int(allow_overlap),
                ),
            )
            request_id = int(cursor.lastrowid)
            self._audit(
                connection,
                admin_id,
                "admin_meeting_started",
                "meeting_request",
                str(request_id),
                {"allow_overlap": allow_overlap, "blocks_calendar": blocks_calendar, "has_guest": bool(email)},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        request = self.get_request(request_id)
        if request is None:
            raise RuntimeError("admin draft not found")
        return request

    def fail_admin_draft(self, request_id: int, reason: str) -> None:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                "UPDATE meeting_requests SET status = ?, updated_at = ? WHERE id = ? AND status = ? AND source = 'ADMIN'",
                (CANCELLED, now, request_id, APPROVING),
            )
            self._audit(connection, None, "admin_meeting_failed", "meeting_request", str(request_id), {"reason": reason})

    def get_settings(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value_json FROM app_settings").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[str(row["key"])] = json.loads(str(row["value_json"]))
            except json.JSONDecodeError:
                continue
        return result

    def set_setting(self, key: str, value: Any, admin_id: int) -> None:
        now = _iso(datetime.now(UTC))
        value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value_json FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
            old_value = str(row["value_json"]) if row else None
            connection.execute(
                """
                INSERT INTO app_settings (key, value_json, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (key, value_json, now, admin_id),
            )
            connection.execute(
                """
                INSERT INTO settings_history (
                    occurred_at, admin_telegram_id, key, old_value_json, new_value_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (now, admin_id, key, old_value, value_json),
            )
            self._audit(
                connection,
                admin_id,
                "setting_changed",
                "setting",
                key,
                {"key": key},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reset_settings(self, admin_id: int) -> int:
        now = _iso(datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT key, value_json FROM app_settings").fetchall()
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO settings_history (
                        occurred_at, admin_telegram_id, key, old_value_json, new_value_json
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (now, admin_id, str(row["key"]), str(row["value_json"])),
                )
            connection.execute("DELETE FROM app_settings")
            self._audit(
                connection,
                admin_id,
                "settings_reset",
                "setting",
                None,
                {"count": len(rows)},
            )
            connection.commit()
            return len(rows)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_setting_history(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT occurred_at, admin_telegram_id, key, old_value_json, new_value_json
                FROM settings_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "occurred_at": _dt(str(row["occurred_at"])),
                    "admin_telegram_id": int(row["admin_telegram_id"]),
                    "key": str(row["key"]),
                    "old_value": json.loads(str(row["old_value_json"])) if row["old_value_json"] else None,
                    "new_value": json.loads(str(row["new_value_json"])) if row["new_value_json"] else None,
                }
            )
        return result

    def add_closed_date(self, local_date: str, admin_id: int) -> bool:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO availability_exceptions (local_date, created_at, created_by)
                VALUES (?, ?, ?)
                """,
                (local_date, now, admin_id),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._audit(
                    connection,
                    admin_id,
                    "closed_date_added",
                    "availability_exception",
                    local_date,
                    {},
                )
            return changed

    def remove_closed_date(self, local_date: str, admin_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM availability_exceptions WHERE local_date = ?",
                (local_date,),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._audit(
                    connection,
                    admin_id,
                    "closed_date_removed",
                    "availability_exception",
                    local_date,
                    {},
                )
            return changed

    def list_closed_dates(self, from_date: str | None = None, limit: int = 100) -> list[str]:
        with self._connect() as connection:
            if from_date is None:
                rows = connection.execute(
                    "SELECT local_date FROM availability_exceptions ORDER BY local_date LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT local_date FROM availability_exceptions
                    WHERE local_date >= ? ORDER BY local_date LIMIT ?
                    """,
                    (from_date, limit),
                ).fetchall()
        return [str(row["local_date"]) for row in rows]

    def schema_info(self) -> tuple[int, str | None]:
        with self._connect() as connection:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            release_row = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'release_id'"
            ).fetchone()
        return int(version_row["version"]), (str(release_row["value"]) if release_row else None)

    def cancel_pending(self, request_id: int, telegram_id: int) -> bool:
        now = _iso(datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE meeting_requests
                SET status = ?, updated_at = ?
                WHERE id = ? AND telegram_id = ? AND status = ?
                """,
                (CANCELLED, now, request_id, telegram_id, PENDING),
            )
            changed = cursor.rowcount == 1
            if changed:
                self._audit(connection, telegram_id, "request_cancelled", "meeting_request", str(request_id), {})
            connection.commit()
            return changed
        finally:
            connection.close()

    def reject(self, request_id: int, admin_id: int) -> MeetingRequest | None:
        now = _iso(datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE meeting_requests SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (REJECTED, now, request_id, PENDING),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            self._audit(connection, admin_id, "request_rejected", "meeting_request", str(request_id), {})
            connection.commit()
        finally:
            connection.close()
        return self.get_request(request_id)

    def claim_for_approval(self, request_id: int, admin_id: int) -> MeetingRequest | None:
        now = _iso(datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE meeting_requests SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (APPROVING, now, request_id, PENDING),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            self._audit(connection, admin_id, "approval_started", "meeting_request", str(request_id), {})
            connection.commit()
        finally:
            connection.close()
        return self.get_request(request_id)

    def reset_approval(self, request_id: int, reason: str) -> None:
        now = _iso(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                "UPDATE meeting_requests SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (PENDING, now, request_id, APPROVING),
            )
            self._audit(connection, None, "approval_reset", "meeting_request", str(request_id), {"reason": reason})

    def complete_approval(self, request_id: int, admin_id: int, google_event_id: str) -> MeetingRequest:
        now = _iso(datetime.now(UTC))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE meeting_requests
                SET status = ?, google_event_id = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (APPROVED, google_event_id, now, request_id, APPROVING),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("request is not being approved")
            self._audit(
                connection,
                admin_id,
                "request_approved",
                "meeting_request",
                str(request_id),
                {"google_event_id": google_event_id},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        request = self.get_request(request_id)
        if request is None:
            raise RuntimeError("approved request not found")
        return request

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor_id: int | None,
        action: str,
        entity_type: str,
        entity_id: str | None,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log (
                occurred_at, actor_telegram_id, action, entity_type, entity_id, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(datetime.now(UTC)),
                actor_id,
                action,
                entity_type,
                entity_id,
                json.dumps(details, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> MeetingRequest:
        return MeetingRequest(
            id=int(row["id"]),
            telegram_id=int(row["telegram_id"]),
            telegram_name=str(row["telegram_name"]),
            telegram_username=row["telegram_username"],
            email=str(row["email"]),
            subject=str(row["subject"]),
            description=row["description"],
            location=row["location"],
            start_at=_dt(row["start_at"]),
            end_at=_dt(row["end_at"]),
            status=str(row["status"]),
            hold_until=_dt(row["hold_until"]),
            google_event_id=row["google_event_id"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            source=str(row["source"]),
            blocks_calendar=bool(row["blocks_calendar"]),
            admin_override=bool(row["admin_override"]),
        )

    @staticmethod
    def _row_to_alternative(row: sqlite3.Row) -> RequestAlternative:
        return RequestAlternative(
            id=int(row["id"]),
            request_id=int(row["request_id"]),
            start_at=_dt(str(row["start_at"])),
            end_at=_dt(str(row["end_at"])),
            status=str(row["status"]),
            hold_until=_dt(str(row["hold_until"])),
            created_by=int(row["created_by"]),
            created_at=_dt(str(row["created_at"])),
            responded_at=_dt(str(row["responded_at"])) if row["responded_at"] else None,
        )

    @staticmethod
    def _row_to_change(row: sqlite3.Row) -> ChangeRequest:
        return ChangeRequest(
            id=int(row["id"]),
            request_id=int(row["request_id"]),
            change_type=str(row["change_type"]),
            proposed_start_at=_dt(str(row["proposed_start_at"])) if row["proposed_start_at"] else None,
            proposed_end_at=_dt(str(row["proposed_end_at"])) if row["proposed_end_at"] else None,
            status=str(row["status"]),
            requested_by=int(row["requested_by"]),
            created_at=_dt(str(row["created_at"])),
            updated_at=_dt(str(row["updated_at"])),
        )

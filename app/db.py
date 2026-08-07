from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.models import (
    APPROVED,
    APPROVING,
    CANCELLED,
    PENDING,
    REJECTED,
    MeetingRequest,
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class SlotConflictError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = FULL;

                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    telegram_name TEXT NOT NULL,
                    telegram_username TEXT,
                    consent_version TEXT,
                    consent_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meeting_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    telegram_name TEXT NOT NULL,
                    telegram_username TEXT,
                    email TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    description TEXT,
                    location TEXT,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    hold_until TEXT NOT NULL,
                    google_event_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_requests_user
                    ON meeting_requests(telegram_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_requests_slot
                    ON meeting_requests(start_at, end_at, status, hold_until);

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    actor_telegram_id INTEGER,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    details_json TEXT NOT NULL
                );
                """
            )

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

    def active_intervals(self, range_start: datetime, range_end: datetime) -> list[tuple[datetime, datetime]]:
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
                """,
                (_iso(range_end), _iso(range_start), APPROVED, PENDING, APPROVING, now),
            ).fetchall()
        return [(_dt(row["start_at"]), _dt(row["end_at"])) for row in rows]

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
                SELECT * FROM meeting_requests
                WHERE telegram_id = ?
                ORDER BY created_at DESC
                LIMIT ?
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
        )

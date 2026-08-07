from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.migrations import MigrationResult, migrate_database
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
        )

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db import Database, _dt, _iso
from app.models import APPROVED, CANCELLED, CANCELLED_BY_ADMIN, JOB_CANCELLED, PENDING


DELETE_CANCEL_FUTURE = "CANCEL_FUTURE"
DELETE_KEEP_FUTURE = "KEEP_FUTURE"
DELETE_REQUESTED = "REQUESTED"
DELETE_PROCESSING = "PROCESSING"
DELETE_WAITING = "WAITING"
DELETE_COMPLETED = "COMPLETED"
DELETE_FAILED = "FAILED"


@dataclass(frozen=True)
class DeletionRequest:
    id: int
    telegram_id: int | None
    mode: str
    status: str
    scope_before: datetime
    execute_after: datetime | None
    future_meeting_count: int


@dataclass(frozen=True)
class PeriodStatistics:
    from_at: datetime
    to_at: datetime
    user_requests: int
    manual_meetings: int
    calendar_meetings: int
    unique_users: int
    statuses: dict[str, int]


class ReleaseDStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    def statistics(self, from_at: datetime, to_at: datetime) -> PeriodStatistics:
        start, end = _iso(from_at), _iso(to_at)
        with self.db._connect() as connection:
            user_requests = int(
                connection.execute(
                    "SELECT COUNT(*) FROM meeting_requests WHERE source = 'USER' AND created_at >= ? AND created_at < ?",
                    (start, end),
                ).fetchone()[0]
            )
            manual_meetings = int(
                connection.execute(
                    "SELECT COUNT(*) FROM meeting_requests WHERE source = 'ADMIN' AND created_at >= ? AND created_at < ?",
                    (start, end),
                ).fetchone()[0]
            )
            one_off = int(
                connection.execute(
                    "SELECT COUNT(*) FROM meeting_requests WHERE google_event_id IS NOT NULL AND start_at >= ? AND start_at < ?",
                    (start, end),
                ).fetchone()[0]
            )
            recurring = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM event_occurrences o
                    JOIN event_series s ON s.id = o.series_id
                    WHERE s.google_series_id IS NOT NULL AND o.actual_start_at >= ? AND o.actual_start_at < ?
                    """,
                    (start, end),
                ).fetchone()[0]
            )
            unique_users = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT telegram_id) FROM meeting_requests
                    WHERE source = 'USER' AND created_at >= ? AND created_at < ? AND telegram_id > 0
                    """,
                    (start, end),
                ).fetchone()[0]
            )
            statuses = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) FROM meeting_requests
                    WHERE source = 'USER' AND created_at >= ? AND created_at < ?
                    GROUP BY status ORDER BY status
                    """,
                    (start, end),
                ).fetchall()
            }
        return PeriodStatistics(
            from_at=from_at,
            to_at=to_at,
            user_requests=user_requests,
            manual_meetings=manual_meetings,
            calendar_meetings=one_off + recurring,
            unique_users=unique_users,
            statuses=statuses,
        )

    def create_deletion_request(self, telegram_id: int, mode: str) -> DeletionRequest:
        if mode not in {DELETE_CANCEL_FUTURE, DELETE_KEEP_FUTURE}:
            raise ValueError("invalid deletion mode")
        now = datetime.now(UTC)
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM deletion_requests
                WHERE telegram_id = ? AND status IN (?, ?, ?)
                ORDER BY id DESC LIMIT 1
                """,
                (telegram_id, DELETE_REQUESTED, DELETE_PROCESSING, DELETE_WAITING),
            ).fetchone()
            if existing:
                connection.commit()
                return self._row(existing)
            future_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM meeting_requests
                    WHERE telegram_id = ? AND status = ? AND end_at > ?
                    """,
                    (telegram_id, APPROVED, _iso(now)),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO deletion_requests (
                    telegram_id, mode, status, scope_before, future_meeting_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, mode, DELETE_REQUESTED, _iso(now), future_count, _iso(now)),
            )
            request_id = int(cursor.lastrowid)
            self.db._audit(connection, telegram_id, "deletion_requested", "deletion_request", str(request_id), {"mode": mode})
            connection.commit()
        finally:
            connection.close()
        request = self.get_deletion_request(request_id)
        if request is None:
            raise RuntimeError("deletion request not found")
        return request

    def get_deletion_request(self, request_id: int) -> DeletionRequest | None:
        with self.db._connect() as connection:
            row = connection.execute("SELECT * FROM deletion_requests WHERE id = ?", (request_id,)).fetchone()
        return self._row(row) if row else None

    def future_google_events(self, request: DeletionRequest) -> list[tuple[int, str]]:
        if request.telegram_id is None:
            return []
        with self.db._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, google_event_id FROM meeting_requests
                WHERE telegram_id = ? AND created_at <= ? AND status = ?
                  AND end_at > ? AND google_event_id IS NOT NULL
                ORDER BY id
                """,
                (request.telegram_id, _iso(request.scope_before), APPROVED, _iso(datetime.now(UTC))),
            ).fetchall()
        return [(int(row["id"]), str(row["google_event_id"])) for row in rows]

    def mark_deletion_failed(self, request_id: int, error_code: str) -> None:
        with self.db._connect() as connection:
            connection.execute(
                "UPDATE deletion_requests SET status = ?, last_error_code = ? WHERE id = ? AND status IN (?, ?)",
                (DELETE_REQUESTED, error_code[:100], request_id, DELETE_REQUESTED, DELETE_PROCESSING),
            )

    def complete_cancel_future(self, request_id: int, telegram_id: int) -> None:
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deletion_requests WHERE id = ? AND telegram_id = ? AND mode = ? AND status = ?",
                (request_id, telegram_id, DELETE_CANCEL_FUTURE, DELETE_REQUESTED),
            ).fetchone()
            if not row:
                raise RuntimeError("deletion request is not executable")
            request = self._row(row)
            connection.execute(
                "UPDATE deletion_requests SET status = ?, confirmed_at = ? WHERE id = ?",
                (DELETE_PROCESSING, _iso(datetime.now(UTC)), request_id),
            )
            self._anonymize_scope(connection, telegram_id, request.scope_before, request_id, keep_future=False)
            completed = _iso(datetime.now(UTC))
            connection.execute(
                """
                UPDATE deletion_requests
                SET telegram_id = NULL, status = ?, completed_at = ?, last_error_code = NULL
                WHERE id = ?
                """,
                (DELETE_COMPLETED, completed, request_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_keep_future(self, request_id: int, telegram_id: int) -> DeletionRequest:
        connection = self.db._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deletion_requests WHERE id = ? AND telegram_id = ? AND mode = ? AND status = ?",
                (request_id, telegram_id, DELETE_KEEP_FUTURE, DELETE_REQUESTED),
            ).fetchone()
            if not row:
                raise RuntimeError("deletion request is not executable")
            request = self._row(row)
            now = datetime.now(UTC)
            last = connection.execute(
                """
                SELECT MAX(end_at) FROM meeting_requests
                WHERE telegram_id = ? AND created_at <= ? AND status = ? AND end_at > ?
                """,
                (telegram_id, _iso(request.scope_before), APPROVED, _iso(now)),
            ).fetchone()[0]
            if last:
                self._anonymize_scope(connection, telegram_id, request.scope_before, request_id, keep_future=True)
                connection.execute(
                    """
                    UPDATE deletion_requests
                    SET status = ?, execute_after = ?, confirmed_at = ?, last_error_code = NULL
                    WHERE id = ?
                    """,
                    (DELETE_WAITING, str(last), _iso(now), request_id),
                )
            else:
                self._anonymize_scope(connection, telegram_id, request.scope_before, request_id, keep_future=False)
                connection.execute(
                    """
                    UPDATE deletion_requests
                    SET telegram_id = NULL, status = ?, confirmed_at = ?, completed_at = ?, last_error_code = NULL
                    WHERE id = ?
                    """,
                    (DELETE_COMPLETED, _iso(now), _iso(now), request_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.get_deletion_request(request_id)
        if result is None:
            raise RuntimeError("deletion request disappeared")
        return result

    def run_retention(self, now: datetime | None = None, months_days: int = 365) -> dict[str, int]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = current - timedelta(days=months_days)
        connection = self.db._connect()
        anonymized = 0
        completed = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            due = connection.execute(
                "SELECT * FROM deletion_requests WHERE status = ? AND execute_after <= ? ORDER BY id",
                (DELETE_WAITING, _iso(current)),
            ).fetchall()
            for row in due:
                request = self._row(row)
                if request.telegram_id is not None:
                    self._anonymize_scope(connection, request.telegram_id, request.scope_before, request.id, keep_future=False)
                connection.execute(
                    "UPDATE deletion_requests SET telegram_id = NULL, status = ?, completed_at = ? WHERE id = ?",
                    (DELETE_COMPLETED, _iso(current), request.id),
                )
                completed += 1

            self._ensure_anonymous_user(connection, -9_000_000_000_000, current)
            old_rows = connection.execute(
                """
                SELECT id FROM meeting_requests
                WHERE created_at < ? AND end_at < ? AND telegram_id > 0
                """,
                (_iso(cutoff), _iso(cutoff)),
            ).fetchall()
            old_ids = [int(row[0]) for row in old_rows]
            if old_ids:
                self._anonymize_request_ids(connection, old_ids, -9_000_000_000_000, current)
                anonymized = len(old_ids)
            connection.execute(
                """
                UPDATE event_series SET created_by = -9000000000000, admin_name = 'Удалено',
                    admin_username = NULL, email = NULL, subject = 'Удалённая встреча',
                    description = NULL, location = NULL, updated_at = ?
                WHERE until_date < ? AND created_by > 0
                """,
                (_iso(current), cutoff.date().isoformat()),
            )
            connection.execute("UPDATE audit_log SET actor_telegram_id = NULL WHERE occurred_at < ?", (_iso(cutoff),))
            connection.execute("DELETE FROM settings_history WHERE occurred_at < ?", (_iso(cutoff),))
            connection.execute(
                "DELETE FROM deletion_requests WHERE status = ? AND completed_at < ?",
                (DELETE_COMPLETED, _iso(cutoff)),
            )
            connection.execute(
                """
                DELETE FROM users WHERE telegram_id > 0 AND updated_at < ?
                  AND NOT EXISTS (SELECT 1 FROM meeting_requests r WHERE r.telegram_id = users.telegram_id)
                """,
                (_iso(cutoff),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"anonymized_requests": anonymized, "completed_deletions": completed}

    def _anonymize_scope(
        self,
        connection: sqlite3.Connection,
        telegram_id: int,
        scope_before: datetime,
        deletion_id: int,
        keep_future: bool,
    ) -> None:
        now = datetime.now(UTC)
        synthetic = -9_000_000_000_000 - deletion_id
        self._ensure_anonymous_user(connection, synthetic, now)
        scoped = connection.execute(
            "SELECT id, status, end_at FROM meeting_requests WHERE telegram_id = ? AND created_at <= ? ORDER BY id",
            (telegram_id, _iso(scope_before)),
        ).fetchall()
        keep_ids = {
            int(row["id"])
            for row in scoped
            if keep_future and str(row["status"]) == APPROVED and _dt(str(row["end_at"])) > now
        }
        anonymize_ids = [int(row["id"]) for row in scoped if int(row["id"]) not in keep_ids]
        if anonymize_ids:
            self._anonymize_request_ids(connection, anonymize_ids, synthetic, now)
            if not keep_future:
                placeholders = ",".join("?" for _ in anonymize_ids)
                connection.execute(
                    f"UPDATE meeting_requests SET status = ?, updated_at = ? WHERE id IN ({placeholders}) AND status = ? AND end_at > ?",
                    (CANCELLED, _iso(now), *anonymize_ids, APPROVED, _iso(now)),
                )
        if keep_ids:
            placeholders = ",".join("?" for _ in keep_ids)
            connection.execute(
                f"""
                UPDATE meeting_requests SET telegram_name = 'Пользователь', telegram_username = NULL,
                    email = 'hidden@example.invalid', subject = 'Встреча', description = NULL,
                    location = NULL, updated_at = ? WHERE id IN ({placeholders})
                """,
                (_iso(now), *sorted(keep_ids)),
            )
        connection.execute("UPDATE audit_log SET actor_telegram_id = NULL WHERE actor_telegram_id = ? AND occurred_at <= ?", (telegram_id, _iso(scope_before)))
        later = connection.execute(
            "SELECT 1 FROM meeting_requests WHERE telegram_id = ? AND created_at > ? LIMIT 1",
            (telegram_id, _iso(scope_before)),
        ).fetchone()
        if not later:
            if keep_ids:
                connection.execute(
                    """
                    UPDATE users SET telegram_name = 'Пользователь', telegram_username = NULL,
                        consent_version = NULL, consent_at = NULL, updated_at = ? WHERE telegram_id = ?
                    """,
                    (_iso(now), telegram_id),
                )
            else:
                connection.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))

    def _anonymize_request_ids(
        self,
        connection: sqlite3.Connection,
        request_ids: list[int],
        synthetic_id: int,
        now: datetime,
    ) -> None:
        if not request_ids:
            return
        placeholders = ",".join("?" for _ in request_ids)
        params = tuple(request_ids)
        connection.execute(
            f"UPDATE notification_deliveries SET error_code = NULL WHERE job_id IN (SELECT id FROM scheduled_jobs WHERE request_id IN ({placeholders}))",
            params,
        )
        connection.execute(
            f"UPDATE scheduled_jobs SET status = ?, recipient_telegram_id = ?, last_error_code = NULL, updated_at = ? WHERE request_id IN ({placeholders})",
            (JOB_CANCELLED, synthetic_id, _iso(now), *params),
        )
        connection.execute(
            f"UPDATE change_requests SET requested_by = ? WHERE request_id IN ({placeholders})",
            (synthetic_id, *params),
        )
        connection.execute(
            f"""
            UPDATE meeting_requests SET telegram_id = ?, telegram_name = 'Удалено', telegram_username = NULL,
                email = 'deleted@example.invalid', subject = 'Удалённая встреча', description = NULL,
                location = NULL,
                status = CASE WHEN status = ? THEN ? ELSE status END,
                updated_at = ? WHERE id IN ({placeholders})
            """,
            (synthetic_id, PENDING, CANCELLED, _iso(now), *params),
        )

    @staticmethod
    def _ensure_anonymous_user(connection: sqlite3.Connection, telegram_id: int, now: datetime) -> None:
        stamp = _iso(now)
        connection.execute(
            """
            INSERT OR IGNORE INTO users (
                telegram_id, telegram_name, telegram_username, consent_version, consent_at, created_at, updated_at
            ) VALUES (?, 'Удалено', NULL, NULL, NULL, ?, ?)
            """,
            (telegram_id, stamp, stamp),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> DeletionRequest:
        return DeletionRequest(
            id=int(row["id"]),
            telegram_id=int(row["telegram_id"]) if row["telegram_id"] is not None else None,
            mode=str(row["mode"]),
            status=str(row["status"]),
            scope_before=_dt(str(row["scope_before"])),
            execute_after=_dt(str(row["execute_after"])) if row["execute_after"] else None,
            future_meeting_count=int(row["future_meeting_count"]),
        )

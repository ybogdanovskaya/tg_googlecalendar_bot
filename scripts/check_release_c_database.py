from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Check release C database state")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database.resolve(strict=True)}?mode=ro", uri=True)
    try:
        schema_row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'release_id'"
        ).fetchone()
        version_row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        result = {
            "ok": True,
            "integrity": connection.execute("PRAGMA quick_check").fetchone()[0],
            "schema_version": int(version_row[0]),
            "release_id": str(schema_row[0]),
            "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
            "requests": int(connection.execute("SELECT COUNT(*) FROM meeting_requests").fetchone()[0]),
            "request_statuses": dict(
                connection.execute(
                    "SELECT status, COUNT(*) FROM meeting_requests GROUP BY status ORDER BY status"
                ).fetchall()
            ),
            "series": int(connection.execute("SELECT COUNT(*) FROM event_series").fetchone()[0]),
            "jobs": dict(
                connection.execute(
                    "SELECT status, COUNT(*) FROM scheduled_jobs GROUP BY status ORDER BY status"
                ).fetchall()
            ),
            "sync_runs": int(connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]),
        }
    finally:
        connection.close()
    if result["integrity"] != "ok" or result["release_id"] != "release-c":
        raise SystemExit("release_c_database_check_failed: " + json.dumps(result, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

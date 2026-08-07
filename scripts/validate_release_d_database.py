from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from app.db import Database


def snapshot(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        return {
            "integrity": integrity,
            "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
            "requests": int(connection.execute("SELECT COUNT(*) FROM meeting_requests").fetchone()[0]),
            "series": int(connection.execute("SELECT COUNT(*) FROM event_series").fetchone()[0]),
            "request_statuses": dict(connection.execute("SELECT status, COUNT(*) FROM meeting_requests GROUP BY status")),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate and validate a copy of the production database for release D")
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit("source_database_missing")
    before = snapshot(args.source)
    with tempfile.TemporaryDirectory(prefix="release-d-db-") as temporary:
        copy_path = Path(temporary) / "calendar_bot.sqlite3"
        with sqlite3.connect(args.source) as source, sqlite3.connect(copy_path) as destination:
            source.backup(destination)
        database = Database(copy_path, Path(temporary) / "backups")
        database.initialize()
        after = snapshot(copy_path)
        schema_version, release_id = database.schema_info()
        with sqlite3.connect(copy_path) as connection:
            deletion_table = bool(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'deletion_requests'"
                ).fetchone()
            )
        preserved = all(before[key] == after[key] for key in ("users", "requests", "series", "request_statuses"))
        result = {
            "ok": before["integrity"] == "ok" and after["integrity"] == "ok" and preserved
            and schema_version == 5 and release_id == "release-d" and deletion_table,
            "before": before,
            "after": after,
            "schema_version": schema_version,
            "release_id": release_id,
            "deletion_table": deletion_table,
            "existing_data_preserved": preserved,
            "migration_backup_created": bool(database.last_migration_result and database.last_migration_result.backup_path),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if not result["ok"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

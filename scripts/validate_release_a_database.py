from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from app.db import Database


def snapshot(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def summary(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    try:
        return {
            "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
            "requests": int(connection.execute("SELECT COUNT(*) FROM meeting_requests").fetchone()[0]),
            "statuses": dict(
                connection.execute(
                    "SELECT status, COUNT(*) FROM meeting_requests GROUP BY status ORDER BY status"
                ).fetchall()
            ),
            "integrity": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate release A migration on a private database copy")
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="calendar-bot-release-a-") as temporary:
        root = Path(temporary)
        copy_path = root / "database-copy.sqlite3"
        snapshot(source, copy_path)
        before = summary(copy_path)
        database = Database(copy_path, root / "migration-backups")
        database.initialize()
        after = summary(copy_path)
        comparable_before = {key: before[key] for key in ("users", "requests", "statuses")}
        comparable_after = {key: after[key] for key in ("users", "requests", "statuses")}
        if comparable_before != comparable_after:
            raise SystemExit("migration_validation_failed: counters changed")
        if after["integrity"] != "ok":
            raise SystemExit(f"migration_validation_failed: integrity={after['integrity']}")
        migration = database.last_migration_result
        print(
            json.dumps(
                {
                    "ok": True,
                    "before": comparable_before,
                    "after": comparable_after,
                    "schema": database.schema_info(),
                    "applied_versions": migration.applied_versions if migration else (),
                    "backup_created": bool(migration and migration.backup_path),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()

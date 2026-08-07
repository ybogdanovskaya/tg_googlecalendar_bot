from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Check release D production database")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        version = int(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
        release = str(connection.execute("SELECT value FROM schema_metadata WHERE key = 'release_id'").fetchone()[0])
        result = {
            "ok": integrity == "ok" and version == 5 and release == "release-d",
            "integrity": integrity,
            "schema_version": version,
            "release_id": release,
            "users": int(connection.execute("SELECT COUNT(*) FROM users WHERE telegram_id > 0").fetchone()[0]),
            "requests": int(connection.execute("SELECT COUNT(*) FROM meeting_requests").fetchone()[0]),
            "series": int(connection.execute("SELECT COUNT(*) FROM event_series").fetchone()[0]),
            "deletion_requests": int(connection.execute("SELECT COUNT(*) FROM deletion_requests").fetchone()[0]),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

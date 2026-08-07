from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--keep-days", type=int, default=7)
    args = parser.parse_args()
    if not args.database.exists():
        raise SystemExit(f"Database not found: {args.database}")
    args.destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = args.destination / f"calendar_bot_{stamp}.sqlite3"
    with sqlite3.connect(args.database) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    cutoff = datetime.now(UTC) - timedelta(days=args.keep_days)
    for candidate in args.destination.glob("calendar_bot_*.sqlite3"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        if modified < cutoff:
            candidate.unlink()
    print(target)


if __name__ == "__main__":
    main()

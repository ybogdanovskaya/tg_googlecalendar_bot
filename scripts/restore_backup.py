from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def verify_database(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"database file not found: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result is None or str(result[0]).lower() != "ok":
        raise RuntimeError(f"database integrity check failed: {path}")


def restore_database(backup_path: Path, target_path: Path) -> Path | None:
    backup_path = backup_path.resolve(strict=True)
    target_path = target_path.resolve(strict=False)
    verify_database(backup_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    safety_path = None
    if target_path.exists():
        verify_database(target_path)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        safety_path = target_path.parent / f"{target_path.stem}.before-restore-{stamp}.sqlite3"
        source = sqlite3.connect(target_path)
        destination = sqlite3.connect(safety_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        verify_database(safety_path)

    temporary_path = target_path.parent / f".{target_path.name}.restore.tmp"
    temporary_path.unlink(missing_ok=True)
    source = sqlite3.connect(backup_path)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    verify_database(temporary_path)
    temporary_path.replace(target_path)
    verify_database(target_path)
    return safety_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a verified SQLite backup")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("restore cancelled: pass --confirm after stopping the bot")
    safety_path = restore_database(args.backup, args.target)
    print(f"restore_ok safety_backup={safety_path or 'not_needed'}")


if __name__ == "__main__":
    main()

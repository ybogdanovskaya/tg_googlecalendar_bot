from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Check local bot data and Google token")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--token", type=Path, required=True)
    args = parser.parse_args()
    if not args.database.exists():
        raise SystemExit("database_missing")
    if not args.token.exists():
        raise SystemExit("google_token_missing")
    with sqlite3.connect(args.database) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise SystemExit(f"database_check_failed: {result}")
    print("ok")


if __name__ == "__main__":
    main()

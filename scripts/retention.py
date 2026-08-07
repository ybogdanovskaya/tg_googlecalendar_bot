from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import Database
from app.release_d_store import ReleaseDStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Anonymize expired personal data and finish due deletion requests")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()
    if not 30 <= args.days <= 3650:
        raise SystemExit("invalid_retention_days")
    database = Database(args.database)
    database.initialize()
    result = ReleaseDStore(database).run_retention(months_days=args.days)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

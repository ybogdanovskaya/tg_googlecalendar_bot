from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Google Apps Script calendar bridge")
    parser.add_argument("--url-file", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    args = parser.parse_args()
    url = args.url_file.read_text(encoding="utf-8").strip()
    secret = args.secret_file.read_text(encoding="utf-8").strip()
    start = datetime.now(UTC)
    payload = {
        "secret": secret,
        "action": "busy",
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=15)).isoformat(),
    }
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("busy"), list):
        raise SystemExit(f"apps_script_check_failed: {result.get('error', 'invalid_response')}")
    print(f"ok busy_intervals={len(result['busy'])}")


if __name__ == "__main__":
    main()

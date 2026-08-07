from __future__ import annotations

import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorize the bot to access one Google Calendar")
    parser.add_argument("--credentials", type=Path, default=Path("secrets/google_credentials.json"))
    parser.add_argument("--token", type=Path, default=Path("secrets/google_token.json"))
    args = parser.parse_args()
    if not args.credentials.exists():
        raise SystemExit(f"OAuth credentials file not found: {args.credentials}")
    flow = InstalledAppFlow.from_client_secrets_file(str(args.credentials), SCOPES)
    credentials = flow.run_local_server(host="localhost", port=0, open_browser=True)
    args.token.parent.mkdir(parents=True, exist_ok=True)
    args.token.write_text(credentials.to_json(), encoding="utf-8")
    print(f"Google token saved to {args.token}")


if __name__ == "__main__":
    main()

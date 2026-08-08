from __future__ import annotations

import re
import subprocess
from pathlib import Path


FORBIDDEN_NAMES = {
    ".env",
    "credentials.json",
    "google_apps_script_secret.txt",
    "google_apps_script_url.txt",
    "telegram_bot_token.txt",
    "token.json",
}

SECRET_PATTERNS = {
    "Telegram bot token": re.compile(rb"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "private key": re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]


def main() -> None:
    failures: list[str] = []
    for path in tracked_files():
        if path.name in FORBIDDEN_NAMES:
            failures.append(f"forbidden secret file: {path}")
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            failures.append(f"cannot read tracked file {path}: {exc}")
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                failures.append(f"possible {label} in {path}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"secret_scan_ok tracked_files={len(tracked_files())}")


if __name__ == "__main__":
    main()

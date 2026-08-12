from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.apps_script_calendar import AppsScriptCalendar
from app.calendar_client import CalendarUnavailable


def verify_sqlite(path: Path, immutable: bool = False) -> None:
    if not path.exists():
        raise RuntimeError("database_missing")
    immutable_option = "&immutable=1" if immutable else ""
    connection = sqlite3.connect(f"file:{path}?mode=ro{immutable_option}", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or str(result[0]).lower() != "ok":
        raise RuntimeError("database_check_failed")


def newest_backup(directory: Path, max_age_hours: int) -> Path:
    candidates = sorted(directory.glob("calendar_bot_*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("backup_missing")
    latest = candidates[0]
    modified = datetime.fromtimestamp(latest.stat().st_mtime, UTC)
    if modified < datetime.now(UTC) - timedelta(hours=max_age_hours):
        raise RuntimeError("backup_stale")
    verify_sqlite(latest, immutable=True)
    return latest


async def verify_apps_script(
    url_file: Path,
    secret_file: Path,
    attempts: int = 5,
    retry_delay_seconds: float = 2,
) -> None:
    if not url_file.exists() or not secret_file.exists():
        raise RuntimeError("apps_script_configuration_missing")
    url = url_file.read_text(encoding="utf-8").strip()
    if not url:
        raise RuntimeError("apps_script_url_empty")
    client = AppsScriptCalendar(url, secret_file)
    if attempts < 1:
        raise ValueError("apps_script_attempts_must_be_positive")
    for attempt in range(attempts):
        now = datetime.now(UTC)
        try:
            await client.busy(now, now + timedelta(minutes=1))
            return
        except CalendarUnavailable:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(retry_delay_seconds * (attempt + 1))


def alert_is_new(state_file: Path | None, failures: list[str]) -> bool:
    """Remember the current failure signature so an outage produces one alert."""
    if state_file is None:
        return True
    signature = "\n".join(sorted(failures))
    try:
        if state_file.read_text(encoding="utf-8") == signature:
            return False
    except FileNotFoundError:
        pass
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(signature, encoding="utf-8")
    return True


def clear_alert_state(state_file: Path | None) -> None:
    if state_file is None:
        return
    try:
        state_file.unlink()
    except FileNotFoundError:
        pass


def send_alert(token_file: Path | None, admin_id: int | None, failures: list[str]) -> None:
    if token_file is None or admin_id is None or not token_file.exists():
        return
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        return
    text = "⚠️ Calendar Bot: ошибка health-check\n" + "\n".join(f"• {item}" for item in failures)
    payload = urllib.parse.urlencode({"chat_id": str(admin_id), "text": text}).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Telegram Calendar Bot health")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--token", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--max-backup-age-hours", type=int, default=48)
    parser.add_argument("--min-free-bytes", type=int, default=2_000_000_000)
    parser.add_argument("--apps-script-url-file", type=Path)
    parser.add_argument("--apps-script-secret-file", type=Path)
    parser.add_argument("--apps-script-attempts", type=int, default=5)
    parser.add_argument("--apps-script-retry-delay-seconds", type=float, default=2)
    parser.add_argument("--alert-token-file", type=Path)
    parser.add_argument("--admin-id", type=int)
    parser.add_argument("--alert-state-file", type=Path)
    args = parser.parse_args()

    checks: dict[str, object] = {}
    failures: list[str] = []

    try:
        verify_sqlite(args.database)
        checks["database"] = "ok"
    except Exception as exc:
        failures.append(f"database:{type(exc).__name__}:{exc}")
        checks["database"] = "failed"

    if args.token:
        checks["token_file"] = "ok" if args.token.exists() and args.token.stat().st_size > 0 else "failed"
        if checks["token_file"] == "failed":
            failures.append("token_file_missing")

    if args.backup_dir:
        try:
            checks["backup"] = str(newest_backup(args.backup_dir, args.max_backup_age_hours).name)
        except Exception as exc:
            failures.append(f"backup:{type(exc).__name__}:{exc}")
            checks["backup"] = "failed"

    free = shutil.disk_usage(args.database.parent).free
    checks["disk_free_bytes"] = free
    if free < args.min_free_bytes:
        failures.append("disk_space_low")

    if args.apps_script_url_file and args.apps_script_secret_file:
        try:
            asyncio.run(
                verify_apps_script(
                    args.apps_script_url_file,
                    args.apps_script_secret_file,
                    args.apps_script_attempts,
                    args.apps_script_retry_delay_seconds,
                )
            )
            checks["apps_script"] = "ok"
        except Exception as exc:
            failures.append(f"apps_script:{type(exc).__name__}")
            checks["apps_script"] = "failed"

    payload = {"ok": not failures, "checks": checks, "failures": failures}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if failures:
        if alert_is_new(args.alert_state_file, failures):
            send_alert(args.alert_token_file, args.admin_id, failures)
        raise SystemExit(1)
    clear_alert_state(args.alert_state_file)


if __name__ == "__main__":
    main()

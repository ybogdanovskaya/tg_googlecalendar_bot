from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная настройка {name}")
    return value


def _secret(name: str, file_name: str) -> str:
    direct = os.getenv(name, "").strip()
    if direct:
        return direct
    secret_path = os.getenv(file_name, "").strip()
    if not secret_path:
        raise RuntimeError(f"Не заданы {name} или {file_name}")
    value = Path(secret_path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Файл секрета пуст: {secret_path}")
    return value


def _optional_file_setting(name: str, file_name: str) -> str:
    direct = os.getenv(name, "").strip()
    if direct:
        return direct
    path = os.getenv(file_name, "").strip()
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_telegram_id: int
    google_calendar_id: str
    google_token_file: Path
    apps_script_url: str
    apps_script_secret_file: Path
    database_path: Path
    log_path: Path
    timezone: str
    min_lead_minutes: int
    booking_horizon_days: int
    hold_hours: int
    privacy_policy_version: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bot_token=_secret("BOT_TOKEN", "BOT_TOKEN_FILE"),
            admin_telegram_id=int(_required("ADMIN_TELEGRAM_ID")),
            google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary").strip() or "primary",
            google_token_file=Path(os.getenv("GOOGLE_TOKEN_FILE", "secrets/google_token.json")),
            apps_script_url=_optional_file_setting("APPS_SCRIPT_URL", "APPS_SCRIPT_URL_FILE"),
            apps_script_secret_file=Path(
                os.getenv("APPS_SCRIPT_SECRET_FILE", "secrets/google_apps_script_secret.txt")
            ),
            database_path=Path(os.getenv("DATABASE_PATH", "data/calendar_bot.sqlite3")),
            log_path=Path(os.getenv("LOG_PATH", "logs/calendar_bot.jsonl")),
            timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
            min_lead_minutes=int(os.getenv("MIN_LEAD_MINUTES", "120")),
            booking_horizon_days=int(os.getenv("BOOKING_HORIZON_DAYS", "30")),
            hold_hours=int(os.getenv("HOLD_HOURS", "24")),
            privacy_policy_version=os.getenv("PRIVACY_POLICY_VERSION", "2026-08-07"),
        )

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

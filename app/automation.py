from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.automation_store import AutomationStore
from app.calendar_client import CalendarClient, CalendarUnavailable
from app.config import Settings
from app.models import (
    APPROVED,
    JOB_MEETING_REMINDER,
    JOB_NEW_REQUEST_NOTIFICATION,
    JOB_PENDING_REMINDER,
    OCCURRENCE_MOVED,
    OCCURRENCE_SCHEDULED,
    PENDING,
)
from app.notification_rules import load_notification_rules


LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 20
SYNC_SECONDS = 15 * 60
BOOTSTRAP_SECONDS = 60 * 60


def _meeting_text(
    subject: str,
    start_at: datetime,
    location: str | None,
    timezone_name: str,
    *,
    all_day: bool = False,
) -> str:
    start = start_at.astimezone(ZoneInfo(timezone_name))
    if all_day:
        text = f"⏰ <b>Напоминание о событии</b>\n\n{start:%d.%m.%Y}\n{html.escape(subject)}"
    else:
        text = f"⏰ <b>Напоминание о встрече</b>\n\n{start:%d.%m.%Y %H:%M} (МСК)\n{html.escape(subject)}"
    if location:
        text += f"\nМесто/ссылка: {html.escape(location)}"
    return text


async def process_due_jobs(
    bot: Bot,
    store: AutomationStore,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(UTC)
    rules = load_notification_rules(store.db, settings)
    processed = 0
    for job in await asyncio.to_thread(store.claim_due_jobs, current, 20):
        try:
            request, occurrence, series = await asyncio.to_thread(store.get_job_subject, job)
            if job.job_type == JOB_MEETING_REMINDER:
                if request:
                    if request.status != APPROVED or request.start_at <= current:
                        await asyncio.to_thread(store.complete_job, job.id, current)
                        continue
                    text = _meeting_text(
                        request.subject,
                        request.start_at,
                        request.location,
                        settings.timezone,
                        all_day=request.all_day,
                    )
                elif occurrence and series:
                    if occurrence.status not in {OCCURRENCE_SCHEDULED, OCCURRENCE_MOVED} or occurrence.actual_start_at <= current:
                        await asyncio.to_thread(store.complete_job, job.id, current)
                        continue
                    text = _meeting_text(series.subject, occurrence.actual_start_at, series.location, settings.timezone)
                else:
                    await asyncio.to_thread(store.complete_job, job.id, current)
                    continue
                await bot.send_message(job.recipient_telegram_id, text)
            elif job.job_type == JOB_PENDING_REMINDER:
                if request is None or request.status != PENDING:
                    await asyncio.to_thread(store.complete_job, job.id, current)
                    continue
                start = request.start_at.astimezone(ZoneInfo(settings.timezone))
                await bot.send_message(
                    job.recipient_telegram_id,
                    f"📌 <b>Заявка №{request.id} всё ещё ожидает решения</b>\n"
                    f"{start:%d.%m.%Y %H:%M} (МСК)",
                )
            elif job.job_type == JOB_NEW_REQUEST_NOTIFICATION:
                if request is None or request.status != PENDING:
                    await asyncio.to_thread(store.complete_job, job.id, current)
                    continue
                start = request.start_at.astimezone(ZoneInfo(settings.timezone))
                await bot.send_message(
                    job.recipient_telegram_id,
                    "📬 <b>Новая заявка на согласование</b>\n\n"
                    f"{html.escape(request.telegram_name)} · {html.escape(request.email)}\n"
                    f"{html.escape(request.subject)}\n"
                    f"{start:%d.%m.%Y %H:%M} (МСК)\n\n"
                    "Откройте раздел «Заявки на рассмотрении» в боте или управление календарём в Mini App.",
                )
            else:
                await asyncio.to_thread(store.complete_job, job.id, current)
                continue
            await asyncio.to_thread(store.complete_job, job.id, current)
            if job.job_type == JOB_PENDING_REMINDER and request:
                await asyncio.to_thread(
                    store.schedule_next_pending_reminder,
                    request.id,
                    settings.admin_telegram_id,
                    rules.pending_reminder_hours,
                    current,
                )
            processed += 1
        except Exception as exc:
            LOGGER.warning(
                "scheduled_job_failed",
                extra={"job_id": job.id, "job_type": job.job_type, "error_type": type(exc).__name__},
            )
            await asyncio.to_thread(store.fail_job, job.id, type(exc).__name__, current)
    return processed


async def reconcile_once(
    bot: Bot,
    store: AutomationStore,
    calendar: CalendarClient,
    settings: Settings,
    limit: int = 20,
) -> dict[str, int]:
    run_id = await asyncio.to_thread(store.start_sync_run)
    checked = changed = missing = errors = 0
    unavailable = False
    rules = load_notification_rules(store.db, settings)
    try:
        candidates = await asyncio.to_thread(store.list_sync_candidates, limit)
        for request in candidates:
            try:
                state = await calendar.event_state(str(request.google_event_id))
                updated, was_changed = await asyncio.to_thread(store.apply_event_state, request.id, state)
                checked += 1
                if was_changed:
                    changed += 1
                    if not state.exists:
                        missing += 1
                    await asyncio.to_thread(
                        store.rebuild_request_reminders,
                        request.id,
                        settings.admin_telegram_id,
                        rules.reminder_minutes,
                    )
                    recipients = {updated.telegram_id, settings.admin_telegram_id}
                    if state.exists:
                        message = f"🔄 Встреча №{updated.id} изменена напрямую в Google Calendar. Данные и напоминания обновлены."
                    else:
                        message = f"❌ Встреча №{updated.id} удалена напрямую из Google Calendar и отменена в боте."
                    for recipient in recipients:
                        await bot.send_message(recipient, message)
            except CalendarUnavailable:
                errors += 1
                unavailable = True
                LOGGER.warning("calendar_reconciliation_unavailable", extra={"request_id": request.id})
                break
            except Exception as exc:
                errors += 1
                LOGGER.exception(
                    "calendar_reconciliation_item_failed",
                    extra={"request_id": request.id, "error_type": type(exc).__name__},
                )
        if not unavailable:
            occurrence_candidates = await asyncio.to_thread(store.list_occurrence_sync_candidates, limit)
            for occurrence, series in occurrence_candidates:
                try:
                    state = await calendar.occurrence_state(str(series.google_series_id), occurrence)
                    updated, updated_series, was_changed = await asyncio.to_thread(
                        store.apply_occurrence_state,
                        occurrence.id,
                        state,
                    )
                    checked += 1
                    if was_changed:
                        changed += 1
                        if not state.exists:
                            missing += 1
                        await asyncio.to_thread(
                            store.rebuild_occurrence_reminders,
                            updated.id,
                            settings.admin_telegram_id,
                            rules.reminder_minutes,
                        )
                        start = updated.actual_start_at.astimezone(ZoneInfo(settings.timezone))
                        if state.exists:
                            message = (
                                f"🔄 Встреча серии №{updated_series.id} на {start:%d.%m.%Y %H:%M} "
                                "изменена напрямую в Google Calendar. Данные и напоминания обновлены."
                            )
                        else:
                            original = occurrence.expected_start_at.astimezone(ZoneInfo(settings.timezone))
                            message = (
                                f"❌ Встреча серии №{updated_series.id} на {original:%d.%m.%Y %H:%M} "
                                "удалена напрямую из Google Calendar и отменена в боте."
                            )
                        await bot.send_message(settings.admin_telegram_id, message)
                except CalendarUnavailable:
                    errors += 1
                    LOGGER.warning(
                        "calendar_occurrence_reconciliation_unavailable",
                        extra={"occurrence_id": occurrence.id},
                    )
                    break
                except Exception as exc:
                    errors += 1
                    LOGGER.exception(
                        "calendar_occurrence_reconciliation_item_failed",
                        extra={"occurrence_id": occurrence.id, "error_type": type(exc).__name__},
                    )
    finally:
        await asyncio.to_thread(store.finish_sync_run, run_id, checked, changed, missing, errors)
    return {"checked": checked, "changed": changed, "missing": missing, "errors": errors}


async def automation_loop(
    bot: Bot,
    store: AutomationStore,
    calendar: CalendarClient,
    settings: Settings,
) -> None:
    last_sync = 0.0
    last_bootstrap = 0.0
    LOGGER.info("automation_loop_started")
    try:
        while True:
            try:
                rules = load_notification_rules(store.db, settings)
                monotonic = time.monotonic()
                if rules.automation_enabled:
                    if monotonic - last_bootstrap >= BOOTSTRAP_SECONDS:
                        summary = await asyncio.to_thread(
                            store.bootstrap_jobs,
                            settings.admin_telegram_id,
                            rules.reminder_minutes,
                            rules.pending_reminder_hours,
                        )
                        LOGGER.info("scheduled_jobs_bootstrapped", extra=summary)
                        last_bootstrap = monotonic
                    await process_due_jobs(bot, store, settings)
                    if monotonic - last_sync >= SYNC_SECONDS:
                        summary = await reconcile_once(bot, store, calendar, settings)
                        LOGGER.info("calendar_reconciliation_completed", extra=summary)
                        last_sync = monotonic
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("automation_iteration_failed")
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        LOGGER.info("automation_loop_stopped")
        raise

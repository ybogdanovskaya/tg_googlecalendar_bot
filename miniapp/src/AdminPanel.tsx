import { useEffect, useState } from "react";

import { ApiError, CalendarApi } from "./api";
import type { AdminChangeRequest, AdminDashboard, AdminSettings, AdminStatistics, CalendarIntegration, EventOccurrence, EventSeries, MeetingRequest } from "./types";

function adminError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Не удалось выполнить действие. Проверьте подключение и повторите попытку.";
}

function dateTime(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

export function AdminPanel({ api }: { api: CalendarApi }) {
  const [dashboard, setDashboard] = useState<AdminDashboard | null>(null);
  const [requests, setRequests] = useState<MeetingRequest[]>([]);
  const [settings, setSettings] = useState<AdminSettings | null>(null);
  const [closedDates, setClosedDates] = useState<string[]>([]);
  const [series, setSeries] = useState<EventSeries[]>([]);
  const [changes, setChanges] = useState<AdminChangeRequest[]>([]);
  const [manualMeetings, setManualMeetings] = useState<MeetingRequest[]>([]);
  const [statistics, setStatistics] = useState<AdminStatistics | null>(null);
  const [integration, setIntegration] = useState<CalendarIntegration | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [alternativeFor, setAlternativeFor] = useState<string | null>(null);
  const [editingRequest, setEditingRequest] = useState<MeetingRequest | null>(null);
  const [editingManualMeeting, setEditingManualMeeting] = useState<MeetingRequest | null>(null);
  const [alternativeStart, setAlternativeStart] = useState("");
  const [closedDate, setClosedDate] = useState("");
  const [manualSubject, setManualSubject] = useState("");
  const [manualEmail, setManualEmail] = useState("");
  const [manualStart, setManualStart] = useState("");
  const [manualDuration, setManualDuration] = useState(30);
  const [seriesSubject, setSeriesSubject] = useState("");
  const [seriesStart, setSeriesStart] = useState("");
  const [seriesUntil, setSeriesUntil] = useState("");
  const [seriesFrequency, setSeriesFrequency] = useState<EventSeries["frequency"]>("WEEKLY");
  const [seriesDuration, setSeriesDuration] = useState(30);
  const [expandedSeriesId, setExpandedSeriesId] = useState<string | null>(null);
  const [occurrences, setOccurrences] = useState<EventOccurrence[]>([]);
  const [movingOccurrenceId, setMovingOccurrenceId] = useState<string | null>(null);
  const [moveOccurrenceStart, setMoveOccurrenceStart] = useState("");
  const [statisticsFrom, setStatisticsFrom] = useState("");
  const [statisticsTo, setStatisticsTo] = useState("");

  const load = () => {
    setLoading(true);
    setError("");
    void Promise.all([api.adminDashboard(), api.adminRequests(), api.adminSettings(), api.adminClosedDates(), api.adminSeries(), api.adminChangeRequests(), api.adminManualMeetings(), api.adminStatistics(), api.adminCalendarIntegration()])
      .then(([nextDashboard, nextRequests, nextSettings, nextClosedDates, nextSeries, nextChanges, nextManualMeetings, nextStatistics, nextIntegration]) => {
        setDashboard(nextDashboard); setRequests(nextRequests); setSettings(nextSettings); setClosedDates(nextClosedDates); setSeries(nextSeries); setChanges(nextChanges); setManualMeetings(nextManualMeetings); setStatistics(nextStatistics); setIntegration(nextIntegration);
      })
      .catch((caught) => setError(adminError(caught)))
      .finally(() => setLoading(false));
  };

  useEffect(load, [api]);

  const act = async (key: string, operation: () => Promise<unknown>) => {
    setBusy(key); setError("");
    try { await operation(); load(); } catch (caught) { setError(adminError(caught)); } finally { setBusy(null); }
  };

  const offerAlternative = async (request: MeetingRequest) => {
    if (!alternativeStart) { setError("Сначала укажите дату и время альтернативы."); return; }
    await act(`alternative-${request.id}`, async () => {
      await api.createAdminAlternative(request.id, new Date(alternativeStart).toISOString(), request.duration_minutes);
      setAlternativeFor(null); setAlternativeStart("");
    });
  };

  const toggleOccurrences = async (seriesId: string) => {
    if (expandedSeriesId === seriesId) { setExpandedSeriesId(null); setOccurrences([]); setMovingOccurrenceId(null); return; }
    setBusy(`occurrences-${seriesId}`); setError("");
    try { setOccurrences(await api.adminSeriesOccurrences(seriesId)); setExpandedSeriesId(seriesId); } catch (caught) { setError(adminError(caught)); } finally { setBusy(null); }
  };

  if (loading && !dashboard) return <section className="section-loading"><span className="spinner" /><p>Загружаем административный кабинет…</p></section>;

  return <section className="admin-panel">
    <div className="section-heading"><div><span className="eyebrow">Только для владельца</span><h1>Управление календарём</h1></div><button className="text-button" onClick={load}>Обновить</button></div>
    {error && <p className="error" role="alert">{error}</p>}
    {dashboard && <div className="admin-metrics" aria-label="Сводка за 30 дней">
      <Metric label="Ожидают решения" value={dashboard.pending_requests} />
      <Metric label="Запросы на изменения" value={dashboard.pending_changes} />
      <Metric label="Пользователей" value={dashboard.statistics.unique_users} />
      <Metric label="Встреч в календаре" value={dashboard.statistics.calendar_meetings} />
    </div>}

    <article className="info-card"><span className="eyebrow">Статистика и интеграция</span><h2>Контроль календаря</h2>{statistics && <p className="muted">За период {statistics.from_date} — {statistics.to_date}: заявок {statistics.user_requests}, ручных встреч {statistics.manual_meetings}, встреч в календаре {statistics.calendar_meetings}, пользователей {statistics.unique_users}.</p>}<form className="inline-form" onSubmit={(event) => { event.preventDefault(); void act("statistics", async () => { setStatistics(await api.adminStatistics(statisticsFrom || undefined, statisticsTo || undefined)); }); }}><input type="date" value={statisticsFrom} onChange={(event) => setStatisticsFrom(event.target.value)} aria-label="Начало периода" /><input type="date" value={statisticsTo} onChange={(event) => setStatisticsTo(event.target.value)} aria-label="Конец периода" /><button className="text-button" disabled={busy !== null}>Обновить период</button></form><p className={integration?.status === "OK" ? "integration-ok" : "error"}>{integration?.status === "OK" ? "Google Calendar доступен." : "Не удалось проверить подключение к Google Calendar."}</p></article>

    <article className="info-card"><div className="section-heading compact"><div><span className="eyebrow">Заявки</span><h2>Ожидают согласования</h2></div><span className="role-badge">{requests.length}</span></div>
      {editingRequest && <MeetingEditor api={api} item={editingRequest} onDone={() => { setEditingRequest(null); load(); }} onCancel={() => setEditingRequest(null)} />}
      {requests.length === 0 ? <p className="muted">Сейчас нет заявок, ожидающих решения.</p> : <div className="request-list">{requests.map((request) => <article className="request-card admin-request" key={request.id}>
        <div className="request-meta"><span>{request.status_label}</span><span>{request.name}</span></div>
        <h2>{request.subject}</h2><p>{dateTime(request.start_at)} · {request.duration_minutes} мин.</p>
        <p className="muted">{request.email}{request.location ? ` · ${request.location}` : ""}</p>
        {request.description && <p className="muted">{request.description}</p>}
        <div className="admin-actions"><button className="button primary" disabled={busy !== null} onClick={() => void act(`approve-${request.id}`, () => api.approveAdminRequest(request.id))}>{busy === `approve-${request.id}` ? "Согласовываем…" : "Согласовать"}</button>
          <button className="text-button" disabled={busy !== null} onClick={() => setEditingRequest(request)}>Редактировать</button>
          <button className="text-button danger" disabled={busy !== null} onClick={() => void act(`reject-${request.id}`, () => api.rejectAdminRequest(request.id))}>Отклонить</button>
          <button className="text-button" disabled={busy !== null} onClick={() => setAlternativeFor(alternativeFor === request.id ? null : request.id)}>Предложить другое время</button></div>
        {alternativeFor === request.id && <div className="admin-alternative"><label>Дата и время<input type="datetime-local" value={alternativeStart} onChange={(event) => setAlternativeStart(event.target.value)} /></label><button className="text-button" disabled={busy !== null} onClick={() => void offerAlternative(request)}>Отправить вариант</button></div>}
      </article>)}</div>}
    </article>

    <article className="info-card"><div className="section-heading compact"><div><span className="eyebrow">Изменения</span><h2>Переносы и отмены</h2></div><span className="role-badge">{changes.length}</span></div>
      {changes.length === 0 ? <p className="muted">Нет запросов на перенос или отмену.</p> : <div className="request-list">{changes.map((item) => <article className="request-card" key={item.change.id}><div className="request-meta"><span>{item.change.change_type === "CANCEL" ? "Отмена" : "Перенос"}</span><span>{item.request.name}</span></div><h2>{item.request.subject}</h2><p>Текущее время: {dateTime(item.request.start_at)}</p>{item.change.change_type === "RESCHEDULE" && item.change.proposed_start_at && <p>Предложено: {dateTime(item.change.proposed_start_at)}</p>}<div className="admin-actions"><button className="button primary" disabled={busy !== null} onClick={() => { if (window.confirm("Выполнить это изменение встречи в календаре?")) void act(`change-approve-${item.change.id}`, () => api.approveAdminChange(item.change.id)); }}>Выполнить</button><button className="text-button danger" disabled={busy !== null} onClick={() => { if (window.confirm("Отклонить запрос пользователя на изменение встречи?")) void act(`change-reject-${item.change.id}`, () => api.rejectAdminChange(item.change.id)); }}>Отклонить</button></div></article>)}</div>}
    </article>

    <article className="info-card"><span className="eyebrow">Ручное создание</span><h2>Добавить встречу</h2><form className="form compact-form" onSubmit={(event) => { event.preventDefault(); if (!manualSubject || !manualStart) return; void act("manual-meeting", async () => { await api.createAdminManualMeeting({ subject: manualSubject, email: manualEmail || undefined, start_at: new Date(manualStart).toISOString(), duration_minutes: manualDuration, blocks_calendar: true, allow_overlap: false }); setManualSubject(""); setManualEmail(""); setManualStart(""); }); }}><label>Тема<input value={manualSubject} maxLength={200} onChange={(event) => setManualSubject(event.target.value)} required /></label><label>Email гостя <span>необязательно</span><input type="email" value={manualEmail} onChange={(event) => setManualEmail(event.target.value)} /></label><div className="form-row"><label>Дата и время<input type="datetime-local" value={manualStart} onChange={(event) => setManualStart(event.target.value)} required /></label><label>Минуты<select value={manualDuration} onChange={(event) => setManualDuration(Number(event.target.value))}>{[15, 30, 45, 60, 90, 120].map((value) => <option key={value}>{value}</option>)}</select></label></div><button className="button primary" disabled={busy !== null}>{busy === "manual-meeting" ? "Создаём…" : "Создать встречу"}</button></form>{editingManualMeeting && <MeetingEditor api={api} item={editingManualMeeting} manual onDone={() => { setEditingManualMeeting(null); load(); }} onCancel={() => setEditingManualMeeting(null)} />}{manualMeetings.length > 0 && <div className="request-list series-list">{manualMeetings.map((item) => <article className="request-card" key={item.id}><div className="request-meta"><span>{item.status_label}</span></div><h2>{item.subject}</h2><p>{dateTime(item.start_at)} · {item.duration_minutes} мин.</p>{item.status === "APPROVED" && <div className="admin-actions"><button className="text-button" disabled={busy !== null} onClick={() => setEditingManualMeeting(item)}>Редактировать</button><button className="text-button danger" disabled={busy !== null} onClick={() => { if (window.confirm("Отменить эту ручную встречу в календаре?")) void act(`manual-${item.id}`, () => api.cancelAdminManualMeeting(item.id)); }}>Отменить встречу</button></div>}</article>)}</div>}</article>

    <article className="info-card"><span className="eyebrow">Повторяющиеся встречи</span><h2>Новая серия</h2><form className="form compact-form" onSubmit={(event) => { event.preventDefault(); if (!seriesSubject || !seriesStart || !seriesUntil) return; void act("series", async () => { await api.createAdminSeries({ subject: seriesSubject, start_at: new Date(seriesStart).toISOString(), duration_minutes: seriesDuration, frequency: seriesFrequency, until_date: seriesUntil, blocks_calendar: true, allow_overlap: false }); setSeriesSubject(""); setSeriesStart(""); setSeriesUntil(""); }); }}><label>Тема<input value={seriesSubject} maxLength={200} onChange={(event) => setSeriesSubject(event.target.value)} required /></label><div className="form-row"><label>Первая встреча<input type="datetime-local" value={seriesStart} onChange={(event) => setSeriesStart(event.target.value)} required /></label><label>Минуты<select value={seriesDuration} onChange={(event) => setSeriesDuration(Number(event.target.value))}>{[15, 30, 45, 60, 90, 120].map((value) => <option key={value}>{value}</option>)}</select></label></div><div className="form-row"><label>Повтор<select value={seriesFrequency} onChange={(event) => setSeriesFrequency(event.target.value as EventSeries["frequency"])}><option value="DAILY">Ежедневно</option><option value="WEEKLY">Еженедельно</option><option value="MONTHLY">Ежемесячно</option></select></label><label>До даты<input type="date" value={seriesUntil} onChange={(event) => setSeriesUntil(event.target.value)} required /></label></div><button className="button primary" disabled={busy !== null}>{busy === "series" ? "Создаём…" : "Создать серию"}</button></form>
      {series.length > 0 && <div className="request-list series-list">{series.map((item) => <article className="request-card" key={item.id}><div className="request-meta"><span>{item.status}</span></div><h2>{item.subject}</h2><p>{dateTime(item.start_at)} · {item.frequency === "DAILY" ? "ежедневно" : item.frequency === "WEEKLY" ? "еженедельно" : "ежемесячно"} до {item.until_date}</p><div className="admin-actions"><button className="text-button" disabled={busy !== null} onClick={() => void toggleOccurrences(item.id)}>{expandedSeriesId === item.id ? "Скрыть повторения" : "Показать повторения"}</button><button className="text-button danger" disabled={busy !== null} onClick={() => { if (window.confirm("Отменить серию и её будущие встречи?")) void act(`series-${item.id}`, () => api.cancelAdminSeries(item.id)); }}>Отменить серию</button></div>{expandedSeriesId === item.id && <div className="occurrence-list">{occurrences.length === 0 ? <p className="muted">Будущих повторений нет.</p> : occurrences.map((occurrence) => <div key={occurrence.id}><span>{dateTime(occurrence.start_at)}</span><div><button className="text-button" disabled={busy !== null} onClick={() => { setMovingOccurrenceId(movingOccurrenceId === occurrence.id ? null : occurrence.id); setMoveOccurrenceStart(""); }}>Перенести</button><button className="text-button danger" disabled={busy !== null} onClick={() => { if (window.confirm("Отменить только это повторение?")) void act(`occurrence-${occurrence.id}`, async () => { await api.cancelAdminOccurrence(item.id, occurrence.id); setExpandedSeriesId(null); setOccurrences([]); }); }}>Отменить</button></div>{movingOccurrenceId === occurrence.id && <div className="occurrence-move"><input type="datetime-local" value={moveOccurrenceStart} onChange={(event) => setMoveOccurrenceStart(event.target.value)} /><button className="text-button" disabled={!moveOccurrenceStart || busy !== null} onClick={() => { if (window.confirm("Перенести только это повторение на выбранное время?")) void act(`move-${occurrence.id}`, async () => { await api.moveAdminOccurrence(item.id, occurrence.id, new Date(moveOccurrenceStart).toISOString(), Math.round((new Date(occurrence.end_at).getTime() - new Date(occurrence.start_at).getTime()) / 60_000)); setExpandedSeriesId(null); setOccurrences([]); setMovingOccurrenceId(null); }); }}>Сохранить время</button></div>}</div>)}</div>}</article>)}</div>}
    </article>

    <article className="info-card"><span className="eyebrow">Правила записи</span><h2>Доступность для пользователей</h2>
      {settings && <><p>{settings.booking.booking_enabled ? "Новые заявки принимаются." : "Новые заявки временно выключены."}</p><button className="button primary" disabled={busy !== null} onClick={() => void act("booking-enabled", () => api.updateAdminSetting("booking_enabled", !settings.booking.booking_enabled))}>{settings.booking.booking_enabled ? "Приостановить запись" : "Открыть запись"}</button>
      <p className="muted">Окно: {String(Math.floor(settings.booking.user_booking_window[0] / 60)).padStart(2, "0")}:{String(settings.booking.user_booking_window[0] % 60).padStart(2, "0")}–{String(Math.floor(settings.booking.user_booking_window[1] / 60)).padStart(2, "0")}:{String(settings.booking.user_booking_window[1] % 60).padStart(2, "0")}; доступные длительности: {settings.booking.durations.join(", ")} мин.</p></>}
    </article>

    {settings && <article className="info-card"><span className="eyebrow">Расширенные настройки</span><h2>Сроки и напоминания</h2><form className="form compact-form" onSubmit={(event) => { event.preventDefault(); const fields = new FormData(event.currentTarget); void act("advanced-settings", () => Promise.all([api.updateAdminSetting("min_lead_minutes", Number(fields.get("min_lead_minutes"))), api.updateAdminSetting("booking_horizon_days", Number(fields.get("booking_horizon_days"))), api.updateAdminSetting("hold_hours", Number(fields.get("hold_hours"))), api.updateAdminSetting("pending_reminder_hours", Number(fields.get("pending_reminder_hours"))), api.updateAdminSetting("reminder_minutes", String(fields.get("reminder_minutes")).split(",").map((value) => Number(value.trim())).filter(Boolean))])); }}><div className="settings-grid"><label>До встречи, минут<input name="min_lead_minutes" type="number" min="0" max="10080" defaultValue={settings.booking.min_lead_minutes} required /></label><label>Горизонт, дней<input name="booking_horizon_days" type="number" min="1" max="365" defaultValue={settings.booking.booking_horizon_days} required /></label><label>Резерв, часов<input name="hold_hours" type="number" min="1" max="168" defaultValue={settings.booking.hold_hours} required /></label><label>Напомнить о заявке, часов<input name="pending_reminder_hours" type="number" min="1" max="168" defaultValue={settings.notifications.pending_reminder_hours} required /></label></div><label>Напоминания о встрече, минут <span>через запятую</span><input name="reminder_minutes" defaultValue={settings.notifications.reminder_minutes.join(", ")} required /></label><button className="button primary" disabled={busy !== null}>{busy === "advanced-settings" ? "Сохраняем…" : "Сохранить настройки"}</button></form><button className="text-button" disabled={busy !== null} onClick={() => void act("automation", () => api.updateAdminSetting("automation_enabled", !settings.notifications.automation_enabled))}>{settings.notifications.automation_enabled ? "Приостановить автоматические напоминания" : "Включить автоматические напоминания"}</button></article>}

    <article className="info-card"><span className="eyebrow">Нерабочие дни</span><h2>Закрытые даты</h2><form className="inline-form" onSubmit={(event) => { event.preventDefault(); if (closedDate) void act("closed-date", async () => { await api.addAdminClosedDate(closedDate); setClosedDate(""); }); }}><input type="date" value={closedDate} onChange={(event) => setClosedDate(event.target.value)} required /><button className="text-button" disabled={busy !== null}>Добавить</button></form>
      {closedDates.length === 0 ? <p className="muted">Закрытых дат нет.</p> : <div className="date-tags">{closedDates.map((value) => <span key={value}>{value}<button aria-label={`Удалить ${value}`} disabled={busy !== null} onClick={() => void act(`remove-${value}`, () => api.removeAdminClosedDate(value))}>×</button></span>)}</div>}
    </article>
  </section>;
}

function Metric({ label, value }: { label: string; value: number }) { return <article><strong>{value}</strong><span>{label}</span></article>; }

function MeetingEditor({ api, item, manual = false, onDone, onCancel }: { api: CalendarApi; item: MeetingRequest; manual?: boolean; onDone: () => void; onCancel: () => void }) {
  const [name, setName] = useState(item.name);
  const [email, setEmail] = useState(item.email);
  const [subject, setSubject] = useState(item.subject);
  const [description, setDescription] = useState(item.description ?? "");
  const [location, setLocation] = useState(item.location ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true); setError("");
    try { await (manual ? api.updateAdminManualMeeting(item.id, { name, email, subject, description: description || null, location: location || null }) : api.updateAdminRequest(item.id, { name, email, subject, description: description || null, location: location || null })); onDone(); } catch (caught) { setError(adminError(caught)); } finally { setBusy(false); }
  };
  return <form className="form admin-editor" onSubmit={(event) => { event.preventDefault(); void save(); }}><h2>Редактировать заявку</h2>{error && <p className="error">{error}</p>}<label>Имя<input value={name} maxLength={120} onChange={(event) => setName(event.target.value)} required /></label><label>Email<input value={email} type="email" maxLength={254} onChange={(event) => setEmail(event.target.value)} required={!manual} /></label><label>Тема<input value={subject} maxLength={200} onChange={(event) => setSubject(event.target.value)} required /></label><label>Описание<textarea value={description} maxLength={4000} onChange={(event) => setDescription(event.target.value)} /></label><label>Место или ссылка<input value={location} maxLength={1000} onChange={(event) => setLocation(event.target.value)} /></label><div className="admin-actions"><button className="button primary" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить"}</button><button type="button" className="text-button" disabled={busy} onClick={onCancel}>Отмена</button></div></form>;
}

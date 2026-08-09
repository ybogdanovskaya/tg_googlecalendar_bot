import { useEffect, useMemo, useState } from "react";

import { ApiError, CalendarApi } from "./api";
import { AdminPanel } from "./AdminPanel";
import { closeTelegramApp, telegramInitData } from "./telegram";
import type { BookingConfig, DeletionRequest, MeetingRequest, RequestAlternative, RequestDraft, SessionInfo } from "./types";

type Screen = "home" | "book" | "requests" | "more" | "admin";

const EMPTY_DRAFT: RequestDraft = {
  durationMinutes: null,
  date: null,
  slot: null,
  name: "",
  email: "",
  subject: "",
  description: "",
  location: ""
};

function localizedDate(value: string, withTime = false): string {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    year: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {})
  }).format(new Date(value));
}

function timeOf(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function isoDate(value: Date): string {
  const offset = value.getTimezoneOffset();
  return new Date(value.getTime() - offset * 60_000).toISOString().slice(0, 10);
}

function friendlyError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "SLOT_UNAVAILABLE") return "Этот слот уже занят. Выберите другое время.";
    if (error.code === "AUTH_REQUIRED" || error.code === "AUTH_INVALID") return "Сессия истекла. Откройте Mini App заново через Telegram.";
    return error.message;
  }
  return "Проверьте подключение и попробуйте ещё раз.";
}

export default function App() {
  const api = useMemo(() => new CalendarApi(), []);
  const [screen, setScreen] = useState<Screen>("home");
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [state, setState] = useState<"loading" | "outside" | "error" | "ready">("loading");
  const [error, setError] = useState("");

  useEffect(() => {
    const initData = telegramInitData();
    if (!initData) {
      setState("outside");
      return;
    }
    void (async () => {
      try {
        const auth = await api.authenticate(initData);
        const current = await api.me();
        setDisplayName(auth.displayName);
        setSession(current);
        setState("ready");
      } catch (caught) {
        setError(friendlyError(caught));
        setState("error");
      }
    })();
  }, [api]);

  if (state === "loading") return <Centered title="Открываем календарь" text="Проверяем защищённую сессию Telegram…" />;
  if (state === "outside") return <Centered title="Откройте приложение в Telegram" text="Для безопасного входа нужны данные, которые Telegram передаёт при запуске Mini App." action="Закрыть" onAction={closeTelegramApp} />;
  if (state === "error") return <Centered title="Не удалось открыть календарь" text={error} action="Закрыть" onAction={closeTelegramApp} />;
  if (!session) return null;
  if (!session.consent.accepted) return <Consent api={api} version={session.consent.version} onAccepted={() => setSession({ ...session, consent: { ...session.consent, accepted: true } })} />;

  return (
    <main className="app-shell">
      <header className="topbar">
        <span className="eyebrow">Календарь встреч</span>
        <span className="role-badge">{session.role === "ADMIN" ? "Пользовательский режим" : "Личный кабинет"}</span>
      </header>
      <section className="page">
        {screen === "home" && <Home name={displayName} onBook={() => setScreen("book")} onRequests={() => setScreen("requests")} />}
        {screen === "book" && <Booking api={api} onDone={() => setScreen("requests")} onBack={() => setScreen("home")} />}
        {screen === "requests" && <Requests api={api} onBook={() => setScreen("book")} />}
        {screen === "more" && <More api={api} policyVersion={session.consent.version} />}
        {screen === "admin" && session.role === "ADMIN" && <AdminPanel api={api} />}
      </section>
      {session.role === "ADMIN" && <div className="admin-nav-link"><button className={`text-button ${screen === "admin" ? "active" : ""}`} onClick={() => setScreen("admin")}>Управление календарём</button></div>}
      <nav className="bottom-nav" aria-label="Основная навигация">
        <NavButton active={screen === "home"} icon="⌂" label="Главная" onClick={() => setScreen("home")} />
        <NavButton active={screen === "requests"} icon="▤" label="Мои встречи" onClick={() => setScreen("requests")} />
        <NavButton active={screen === "more"} icon="•••" label="Ещё" onClick={() => setScreen("more")} />
      </nav>
    </main>
  );
}

function Centered({ title, text, action, onAction }: { title: string; text: string; action?: string; onAction?: () => void }) {
  return <main className="centered"><div className="brand-mark">К</div><h1>{title}</h1><p>{text}</p>{action && <button className="button primary" onClick={onAction}>{action}</button>}</main>;
}

function Consent({ api, version, onAccepted }: { api: CalendarApi; version: string; onAccepted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const accept = async () => {
    setBusy(true); setError("");
    try { await api.acceptConsent(); onAccepted(); } catch (caught) { setError(friendlyError(caught)); } finally { setBusy(false); }
  };
  return <main className="centered policy"><div className="brand-mark">К</div><h1>Конфиденциальность</h1><p>Мы используем ваши контактные данные только для обработки заявки, уведомлений и записи на встречу. Содержимое личного Google Calendar не передаётся Mini App.</p><p className="muted">Версия политики: {version}</p>{error && <p className="error" role="alert">{error}</p>}<button className="button primary" disabled={busy} onClick={() => void accept()}>{busy ? "Сохраняем…" : "Согласен"}</button></main>;
}

function Home({ name, onBook, onRequests }: { name: string; onBook: () => void; onRequests: () => void }) {
  return <><section className="hero"><p className="eyebrow">Добро пожаловать{name ? `, ${name}` : ""}</p><h1>Выберите удобное время для встречи</h1><p>Свободные слоты всегда проверяются на сервере — без раскрытия личного календаря.</p><button className="button primary" onClick={onBook}>Записаться на встречу</button></section><section className="info-card"><span>Мои встречи</span><p>Проверьте статус заявки, измените или отмените её, пока резерв активен.</p><button className="text-button" onClick={onRequests}>Открыть список →</button></section></>;
}

function Booking({ api, onDone, onBack }: { api: CalendarApi; onDone: () => void; onBack: () => void }) {
  const [config, setConfig] = useState<BookingConfig | null>(null);
  const [draft, setDraft] = useState<RequestDraft>(EMPTY_DRAFT);
  const [step, setStep] = useState(1);
  const [dates, setDates] = useState<string[]>([]);
  const [slots, setSlots] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { void api.bookingConfig().then(setConfig).catch((caught) => setError(friendlyError(caught))).finally(() => setLoading(false)); }, [api]);
  useEffect(() => {
    if (!config || !config.booking_enabled) return;
    const from = isoDate(new Date());
    const to = isoDate(new Date(Date.now() + (config.horizon_days - 1) * 86_400_000));
    setLoading(true); setError("");
    void api.bookingCalendar(from, to).then((calendar) => setDates(calendar.available_dates)).catch((caught) => setError(friendlyError(caught))).finally(() => setLoading(false));
  }, [api, config]);
  useEffect(() => {
    if (!draft.date || !draft.durationMinutes) return;
    setLoading(true); setError("");
    void api.bookingSlots(draft.date, draft.durationMinutes).then((response) => setSlots(response.slots)).catch((caught) => setError(friendlyError(caught))).finally(() => setLoading(false));
  }, [api, draft.date, draft.durationMinutes]);

  const submit = async () => {
    if (!draft.slot || !draft.durationMinutes) return;
    setSubmitting(true); setError("");
    try {
      await api.createRequest({ name: draft.name, email: draft.email, subject: draft.subject, description: draft.description || null, location: draft.location || null, start_at: draft.slot, duration_minutes: draft.durationMinutes });
      onDone();
    } catch (caught) { setError(friendlyError(caught)); if (caught instanceof ApiError && caught.code === "SLOT_UNAVAILABLE") setStep(3); } finally { setSubmitting(false); }
  };

  if (loading && !config) return <SectionLoading label="Загружаем правила записи…" />;
  if (!config) return <ErrorBlock text={error} onRetry={onBack} />;
  if (!config.booking_enabled) return <section className="flow"><div className="flow-heading"><button className="back" onClick={onBack}>←</button><div><span className="eyebrow">Запись</span><h1>Запись временно недоступна</h1></div></div><Empty label="Новые заявки временно выключены администратором. Попробуйте позже." /></section>;
  return <section className="flow"><div className="flow-heading"><button className="back" onClick={onBack}>←</button><div><span className="eyebrow">Запись · шаг {step} из 5</span><h1>{["Выберите длительность", "Выберите дату", "Выберите время", "Расскажите о встрече", "Проверьте заявку"][step - 1]}</h1></div></div><div className="progress"><i style={{ width: `${step * 20}%` }} /></div>{error && <p className="error" role="alert">{error}</p>}
    {step === 1 && <div className="choice-grid">{config.durations.map((duration) => <button className={`choice ${draft.durationMinutes === duration ? "selected" : ""}`} key={duration} onClick={() => { setDraft({ ...draft, durationMinutes: duration }); setStep(2); }}>{duration} минут</button>)}</div>}
    {step === 2 && <div className="date-grid">{dates.map((date) => <button key={date} className={`date-button ${draft.date === date ? "selected" : ""}`} onClick={() => { setDraft({ ...draft, date, slot: null }); setStep(3); }}><strong>{new Intl.DateTimeFormat("ru-RU", { day: "numeric" }).format(new Date(`${date}T12:00:00`))}</strong><span>{new Intl.DateTimeFormat("ru-RU", { weekday: "short" }).format(new Date(`${date}T12:00:00`))}</span></button>)}</div>}
    {step === 3 && <>{loading ? <SectionLoading label="Проверяем свободное время…" /> : slots.length ? <div className="slot-grid">{slots.map((slot) => <button key={slot} className={`slot-button ${draft.slot === slot ? "selected" : ""}`} onClick={() => { setDraft({ ...draft, slot }); setStep(4); }}>{timeOf(slot)}</button>)}</div> : <Empty label="На выбранную дату нет свободного времени." />}</>}
    {step === 4 && <MeetingForm draft={draft} onChange={setDraft} onNext={() => setStep(5)} />}
    {step === 5 && <div className="review-card"><p className="status-line">На согласовании</p><h2>{draft.subject}</h2><p>{draft.slot && localizedDate(draft.slot, true)} · {draft.durationMinutes} минут</p><p>{draft.name} · {draft.email}</p>{draft.location && <p>{draft.location}</p>}<p className="muted">Слот будет зарезервирован на срок, установленный правилами записи.</p><button className="button primary" disabled={submitting} onClick={() => void submit()}>{submitting ? "Отправляем…" : "Отправить заявку"}</button></div>}
  </section>;
}

function MeetingForm({ draft, onChange, onNext }: { draft: RequestDraft; onChange: (value: RequestDraft) => void; onNext: () => void }) {
  const valid = draft.name.trim() && /^\S+@\S+\.\S+$/.test(draft.email) && draft.subject.trim();
  const update = (field: keyof RequestDraft, value: string) => onChange({ ...draft, [field]: value });
  return <form className="form" onSubmit={(event) => { event.preventDefault(); if (valid) onNext(); }}><label>Имя<input value={draft.name} maxLength={120} onChange={(event) => update("name", event.target.value)} required /></label><label>Email<input value={draft.email} type="email" maxLength={254} onChange={(event) => update("email", event.target.value)} required /></label><label>Тема встречи<input value={draft.subject} maxLength={200} onChange={(event) => update("subject", event.target.value)} required /></label><label>Описание <span>необязательно</span><textarea value={draft.description} maxLength={4000} onChange={(event) => update("description", event.target.value)} /></label><label>Место или ссылка <span>необязательно</span><input value={draft.location} maxLength={1000} onChange={(event) => update("location", event.target.value)} /></label><button className="button primary" disabled={!valid}>Продолжить</button></form>;
}

function Requests({ api, onBook }: { api: CalendarApi; onBook: () => void }) {
  const [items, setItems] = useState<MeetingRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editing, setEditing] = useState<MeetingRequest | null>(null);
  const [alternativeFor, setAlternativeFor] = useState<MeetingRequest | null>(null);
  const [alternatives, setAlternatives] = useState<RequestAlternative[]>([]);
  const [rescheduling, setRescheduling] = useState<MeetingRequest | null>(null);
  const load = () => { setLoading(true); setError(""); void api.requests().then(setItems).catch((caught) => setError(friendlyError(caught))).finally(() => setLoading(false)); };
  useEffect(load, [api]);
  const cancel = async (item: MeetingRequest) => { if (!window.confirm(`Отменить заявку «${item.subject}»?`)) return; setBusyId(item.id); try { await api.cancelRequest(item.id); load(); } catch (caught) { setError(friendlyError(caught)); } finally { setBusyId(null); } };
  const showAlternatives = async (item: MeetingRequest) => { setBusyId(item.id); setError(""); try { setAlternatives(await api.alternatives(item.id)); setAlternativeFor(item); } catch (caught) { setError(friendlyError(caught)); } finally { setBusyId(null); } };
  const requestCancel = async (item: MeetingRequest) => { if (!window.confirm("Отправить администратору запрос на отмену? Встреча останется в календаре до решения.")) return; setBusyId(item.id); try { await api.createChangeRequest(item.id, { change_type: "CANCEL" }); load(); } catch (caught) { setError(friendlyError(caught)); } finally { setBusyId(null); } };
  if (loading) return <SectionLoading label="Загружаем ваши встречи…" />;
  return <section><div className="section-heading"><div><span className="eyebrow">Личный кабинет</span><h1>Мои встречи</h1></div><button className="text-button" onClick={load}>Обновить</button></div>{error && <p className="error" role="alert">{error}</p>}{editing && <EditRequest api={api} item={editing} onDone={() => { setEditing(null); load(); }} onCancel={() => setEditing(null)} />}{alternativeFor && <Alternatives api={api} item={alternativeFor} alternatives={alternatives} onDone={() => { setAlternativeFor(null); load(); }} onCancel={() => setAlternativeFor(null)} />}{rescheduling && <Reschedule api={api} item={rescheduling} onDone={() => { setRescheduling(null); load(); }} onCancel={() => setRescheduling(null)} />}{items.length === 0 ? <Empty label="Пока нет заявок на встречу." action="Записаться" onAction={onBook} /> : <div className="request-list">{items.map((item) => <article className="request-card" key={item.id}><div className="request-meta"><span>{item.status_label}</span>{item.reservation.active && <span>Резерв активен</span>}</div><h2>{item.subject}</h2><p>{localizedDate(item.start_at, true)} · {item.duration_minutes} минут</p>{item.location && <p className="muted">{item.location}</p>}{item.open_change && <p className="change-pending">{item.open_change.change_type === "RESCHEDULE" ? <>Запрос на перенос ожидает решения: {item.open_change.proposed_start_at && localizedDate(item.open_change.proposed_start_at, true)}.</> : "Запрос на отмену встречи ожидает решения."}</p>}<div className="request-actions">{item.allowed_actions.includes("EDIT") && <><button className="text-button" onClick={() => setEditing(item)}>Изменить данные</button><button className="text-button" onClick={() => setRescheduling(item)}>Изменить время</button></>}{item.status === "PENDING" && <button className="text-button" disabled={busyId === item.id} onClick={() => void showAlternatives(item)}>Проверить альтернативы</button>}{item.allowed_actions.includes("CANCEL") && <button className="text-button danger" disabled={busyId === item.id} onClick={() => void cancel(item)}>{busyId === item.id ? "Отменяем…" : "Отменить заявку"}</button>}{item.allowed_actions.includes("REQUEST_RESCHEDULE") && <button className="text-button" onClick={() => setRescheduling(item)}>Запросить перенос</button>}{item.allowed_actions.includes("REQUEST_CANCEL") && <button className="text-button danger" disabled={busyId === item.id} onClick={() => void requestCancel(item)}>Запросить отмену встречи</button>}</div></article>)}</div>}</section>;
}

function EditRequest({ api, item, onDone, onCancel }: { api: CalendarApi; item: MeetingRequest; onDone: () => void; onCancel: () => void }) {
  const [name, setName] = useState(item.name); const [email, setEmail] = useState(item.email); const [subject, setSubject] = useState(item.subject); const [description, setDescription] = useState(item.description ?? ""); const [location, setLocation] = useState(item.location ?? ""); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const save = async () => { setBusy(true); try { await api.updateRequest(item.id, { name, email, subject, description: description || null, location: location || null }); onDone(); } catch (caught) { setError(friendlyError(caught)); } finally { setBusy(false); } };
  return <article className="review-card"><h2>Изменить заявку</h2>{error && <p className="error">{error}</p>}<div className="form"><label>Имя<input value={name} onChange={(event) => setName(event.target.value)} /></label><label>Email<input value={email} onChange={(event) => setEmail(event.target.value)} /></label><label>Тема<input value={subject} onChange={(event) => setSubject(event.target.value)} /></label><label>Описание<textarea value={description} onChange={(event) => setDescription(event.target.value)} /></label><label>Место или ссылка<input value={location} onChange={(event) => setLocation(event.target.value)} /></label><button className="button primary" disabled={busy || !name || !subject || !/^\S+@\S+\.\S+$/.test(email)} onClick={() => void save()}>{busy ? "Сохраняем…" : "Сохранить"}</button><button className="text-button" onClick={onCancel}>Отмена</button></div></article>;
}

function Alternatives({ api, item, alternatives, onDone, onCancel }: { api: CalendarApi; item: MeetingRequest; alternatives: RequestAlternative[]; onDone: () => void; onCancel: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const accept = async (alternative: RequestAlternative) => { setBusy(true); try { await api.acceptAlternative(item.id, alternative.id); onDone(); } catch (caught) { setError(friendlyError(caught)); } finally { setBusy(false); } };
  const decline = async () => { setBusy(true); try { await api.declineAlternatives(item.id); onDone(); } catch (caught) { setError(friendlyError(caught)); } finally { setBusy(false); } };
  return <article className="review-card"><h2>Предложенное время</h2>{error && <p className="error">{error}</p>}{alternatives.length ? alternatives.map((alternative) => <div className="info-card" key={alternative.id}><p>{localizedDate(alternative.start_at, true)} · {alternative.duration_minutes} минут</p><p className="muted">Доступно до {localizedDate(alternative.expires_at, true)}</p><button className="button primary" disabled={busy} onClick={() => void accept(alternative)}>Выбрать это время</button></div>) : <p>Активных альтернатив пока нет.</p>}<button className="text-button danger" disabled={busy || !alternatives.length} onClick={() => void decline()}>Ни один вариант не подходит</button><button className="text-button" onClick={onCancel}>Закрыть</button></article>;
}

function Reschedule({ api, item, onDone, onCancel }: { api: CalendarApi; item: MeetingRequest; onDone: () => void; onCancel: () => void }) {
  const [date, setDate] = useState(""); const [slots, setSlots] = useState<string[]>([]); const [slot, setSlot] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const loadSlots = async () => { if (!date) return; setBusy(true); setError(""); try { setSlots((await api.bookingSlots(date, item.duration_minutes)).slots); } catch (caught) { setError(friendlyError(caught)); } finally { setBusy(false); } };
  const pending = item.status === "PENDING";
  const send = async () => { if (!slot) return; setBusy(true); try { if (pending) await api.updateRequest(item.id, { start_at: slot, duration_minutes: item.duration_minutes }); else await api.createChangeRequest(item.id, { change_type: "RESCHEDULE", start_at: slot, duration_minutes: item.duration_minutes }); onDone(); } catch (caught) { setError(friendlyError(caught)); } finally { setBusy(false); } };
  return <article className="review-card"><h2>{pending ? "Изменить время" : "Запросить перенос"}</h2><p>{pending ? "Новое время будет сохранено после серверной проверки." : "Старая встреча останется в силе, пока администратор не примет решение."}</p>{error && <p className="error">{error}</p>}<div className="form"><label>Новая дата<input type="date" value={date} onChange={(event) => { setDate(event.target.value); setSlot(""); setSlots([]); }} /></label><button className="text-button" disabled={!date || busy} onClick={() => void loadSlots()}>Показать свободное время</button>{slots.length > 0 && <div className="slot-grid">{slots.map((value) => <button className={`slot-button ${slot === value ? "selected" : ""}`} key={value} onClick={() => setSlot(value)}>{timeOf(value)}</button>)}</div>}<button className="button primary" disabled={!slot || busy} onClick={() => void send()}>{busy ? "Отправляем…" : pending ? "Сохранить время" : "Отправить запрос"}</button><button className="text-button" onClick={onCancel}>Отмена</button></div></article>;
}

function More({ api, policyVersion }: { api: CalendarApi; policyVersion: string }) {
  const [deletion, setDeletion] = useState<DeletionRequest | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const prepare = async (mode: DeletionRequest["mode"]) => { setBusy(true); setError(""); try { setDeletion(await api.createDeletionRequest(mode)); } catch (caught) { setError(friendlyError(caught)); } finally { setBusy(false); } };
  const confirm = async () => { if (!deletion) return; setBusy(true); try { setDeletion(await api.confirmDeletionRequest(deletion.id)); } catch (caught) { setError(friendlyError(caught)); } finally { setBusy(false); } };
  return <section><span className="eyebrow">Справка</span><h1>Ещё</h1><article className="info-card"><h2>Конфиденциальность</h2><p>Версия политики: {policyVersion}. Данные встречи используются только для её организации и уведомлений.</p></article><article className="info-card"><h2>Удаление данных</h2>{error && <p className="error">{error}</p>}{!deletion && <><p>Выберите вариант; перед окончательным выполнением будут показаны последствия.</p><button className="text-button danger" disabled={busy} onClick={() => void prepare("CANCEL_FUTURE")}>Удалить данные и отменить будущие встречи</button><button className="text-button" disabled={busy} onClick={() => void prepare("KEEP_FUTURE")}>Удалить историю, будущие встречи сохранить</button></>}{deletion?.status === "REQUESTED" && <><p>Будущих согласованных встреч: {deletion.future_meeting_count}. Подтвердите необратимое действие.</p><button className="button primary" disabled={busy} onClick={() => void confirm()}>{busy ? "Выполняем…" : "Подтвердить"}</button></>}{deletion && deletion.status !== "REQUESTED" && <p>Запрос обработан: {deletion.status}.</p>}</article><article className="info-card"><h2>Нужна помощь?</h2><p>Откройте чат-бот и выберите «Помощь». Там по-прежнему доступны все действующие сценарии.</p></article></section>;
}

function SectionLoading({ label }: { label: string }) { return <section className="section-loading"><span className="spinner" /><p>{label}</p></section>; }
function Empty({ label, action, onAction }: { label: string; action?: string; onAction?: () => void }) { return <div className="empty"><p>{label}</p>{action && <button className="button primary" onClick={onAction}>{action}</button>}</div>; }
function ErrorBlock({ text, onRetry }: { text: string; onRetry: () => void }) { return <div className="empty"><p className="error">{text}</p><button className="button primary" onClick={onRetry}>Вернуться</button></div>; }
function NavButton({ active, icon, label, onClick }: { active: boolean; icon: string; label: string; onClick: () => void }) { return <button className={`nav-button ${active ? "active" : ""}`} onClick={onClick}><span>{icon}</span>{label}</button>; }

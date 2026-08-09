import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const NOW = "2026-08-08T12:00:00+03:00";

function json(value: unknown): Response {
  return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
}

function installTelegram(): void {
  window.Telegram = { WebApp: { initData: "signed-test-data", ready: vi.fn(), expand: vi.fn(), close: vi.fn() } };
}

function installApi(role: "USER" | "ADMIN", bookingEnabled = true): void {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/auth/telegram")) return json({ user: { display_name: "Тестовый пользователь", role, consent: { accepted: true, version: "test" } }, csrf_token: "csrf-token", expires_at: NOW });
    if (path.endsWith("/me")) return json({ role, consent: { accepted: true, version: "test" }, timezone: "Europe/Moscow", expires_at: NOW });
    if (path.endsWith("/booking/config")) return json({ timezone: "Europe/Moscow", booking_enabled: bookingEnabled, durations: [30], step_minutes: 30, horizon_days: 30, min_lead_minutes: 60, window: { start_minutes: 480, end_minutes: 1260 } });
    if (path.endsWith("/requests")) return json({ items: [] });
    if (path.endsWith("/admin/dashboard")) return json({ pending_requests: 0, pending_changes: 0, statistics: { user_requests: 0, manual_meetings: 0, calendar_meetings: 0, unique_users: 1 } });
    if (path.endsWith("/admin/requests") || path.endsWith("/admin/series") || path.endsWith("/admin/change-requests") || path.endsWith("/admin/manual-meetings") || path.endsWith("/admin/closed-dates")) return json({ items: [] });
    if (path.endsWith("/admin/settings")) return json({ booking: { booking_enabled: true, min_lead_minutes: 60, booking_horizon_days: 30, hold_hours: 24, durations: [30], step_minutes: 30, user_booking_window: [480, 1260] }, notifications: { reminder_minutes: [60], pending_reminder_hours: 12, automation_enabled: true } });
    if (path.endsWith("/admin/statistics")) return json({ from_date: "2026-07-09", to_date: "2026-08-08", user_requests: 0, manual_meetings: 0, calendar_meetings: 0, unique_users: 1 });
    if (path.endsWith("/admin/integration/calendar")) return json({ status: "OK", checked_at: NOW });
    throw new Error(`Unexpected request: ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
}

describe("локальный UAT экранов", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    delete window.Telegram;
  });

  it("не открывает кабинет вне Telegram", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "Откройте приложение в Telegram" })).toBeInTheDocument();
    expect(screen.queryByText("Управление календарём")).not.toBeInTheDocument();
  });

  it("открывает пользовательский кабинет и пустой список только после Telegram-аутентификации", async () => {
    installTelegram();
    installApi("USER");
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Выберите удобное время для встречи" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Мои встречи/ }));
    expect(await screen.findByText("Пока нет актуальных заявок на встречу.")).toBeInTheDocument();
    expect(screen.queryByText("Управление календарём")).not.toBeInTheDocument();
  });

  it("не даёт начать запись, когда администратор её приостановил", async () => {
    installTelegram();
    installApi("USER", false);
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Записаться на встречу" }));
    expect(await screen.findByRole("heading", { name: "Запись временно недоступна" })).toBeInTheDocument();
    expect(screen.getByText("Новые заявки временно выключены администратором. Попробуйте позже.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "30 минут" })).not.toBeInTheDocument();
  });

  it("показывает административный экран только для роли, пришедшей от API", async () => {
    installTelegram();
    installApi("ADMIN");
    render(<App />);

    const adminNavigation = await screen.findByRole("button", { name: "Управление календарём" });
    fireEvent.click(adminNavigation);
    expect(await screen.findByRole("heading", { name: "Управление календарём" })).toBeInTheDocument();
    expect(await screen.findByText("Google Calendar доступен.")).toBeInTheDocument();
  });
});

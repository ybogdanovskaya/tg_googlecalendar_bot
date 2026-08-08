import { afterEach, describe, expect, it, vi } from "vitest";

import { CalendarApi } from "./api";

describe("CalendarApi", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends Telegram initData only to the authentication endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      user: { display_name: "Тест", role: "USER", consent: { accepted: true, version: "test" } },
      csrf_token: "csrf-token",
      expires_at: "2026-08-08T12:30:00+03:00"
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const api = new CalendarApi();
    const result = await api.authenticate("telegram-signed-data");

    expect(result.displayName).toBe("Тест");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/auth/telegram", expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      body: JSON.stringify({ init_data: "telegram-signed-data" })
    }));
  });
});

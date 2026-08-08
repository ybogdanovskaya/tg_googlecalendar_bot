import type {
  BookingCalendar,
  BookingConfig,
  BookingSlots,
  ChangeRequest,
  DeletionRequest,
  MeetingRequest,
  RequestAlternative,
  SessionInfo
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

interface ApiErrorPayload {
  error?: { code?: string; message?: string; retryable?: boolean };
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryable = false
  ) {
    super(message);
  }
}

export class CalendarApi {
  private csrfToken = "";

  async authenticate(initData: string): Promise<{ session: SessionInfo; displayName: string }> {
    const response = await this.request<{ user: { display_name: string; role: SessionInfo["role"]; consent: SessionInfo["consent"] }; csrf_token: string; expires_at: string }>(
      "/auth/telegram",
      { method: "POST", body: { init_data: initData }, csrf: false }
    );
    this.csrfToken = response.csrf_token;
    return {
      displayName: response.user.display_name,
      session: {
        role: response.user.role,
        consent: response.user.consent,
        timezone: "Europe/Moscow",
        expires_at: response.expires_at
      }
    };
  }

  async me(): Promise<SessionInfo> {
    return this.request<SessionInfo>("/me");
  }

  async acceptConsent(): Promise<void> {
    await this.request("/consents", { method: "POST", body: { accepted: true }, mutation: true });
  }

  async bookingConfig(): Promise<BookingConfig> {
    return this.request<BookingConfig>("/booking/config");
  }

  async bookingCalendar(from: string, to: string): Promise<BookingCalendar> {
    return this.request<BookingCalendar>(`/booking/calendar?from_date=${encodeURIComponent(from)}&to_date=${encodeURIComponent(to)}`);
  }

  async bookingSlots(date: string, durationMinutes: number): Promise<BookingSlots> {
    return this.request<BookingSlots>(`/booking/slots?date=${encodeURIComponent(date)}&duration_minutes=${durationMinutes}`);
  }

  async requests(): Promise<MeetingRequest[]> {
    const response = await this.request<{ items: MeetingRequest[] }>("/requests");
    return response.items;
  }

  async createRequest(payload: {
    name: string;
    email: string;
    subject: string;
    description: string | null;
    location: string | null;
    start_at: string;
    duration_minutes: number;
  }): Promise<MeetingRequest> {
    return this.request<MeetingRequest>("/requests", { method: "POST", body: payload, mutation: true });
  }

  async cancelRequest(requestId: string): Promise<void> {
    await this.request(`/requests/${encodeURIComponent(requestId)}/cancel`, { method: "POST", body: {}, mutation: true });
  }

  async updateRequest(requestId: string, payload: Partial<Pick<MeetingRequest, "name" | "email" | "subject" | "description" | "location" | "start_at" | "duration_minutes">>): Promise<MeetingRequest> {
    return this.request<MeetingRequest>(`/requests/${encodeURIComponent(requestId)}`, { method: "PATCH", body: payload, mutation: true });
  }

  async alternatives(requestId: string): Promise<RequestAlternative[]> {
    const response = await this.request<{ items: RequestAlternative[] }>(`/requests/${encodeURIComponent(requestId)}/alternatives`);
    return response.items;
  }

  async acceptAlternative(requestId: string, alternativeId: string): Promise<MeetingRequest> {
    return this.request<MeetingRequest>(`/requests/${encodeURIComponent(requestId)}/alternatives/${encodeURIComponent(alternativeId)}/accept`, { method: "POST", body: {}, mutation: true });
  }

  async declineAlternatives(requestId: string): Promise<void> {
    await this.request(`/requests/${encodeURIComponent(requestId)}/alternatives/decline`, { method: "POST", body: {}, mutation: true });
  }

  async createChangeRequest(requestId: string, payload: { change_type: "CANCEL" | "RESCHEDULE"; start_at?: string; duration_minutes?: number }): Promise<ChangeRequest> {
    return this.request<ChangeRequest>(`/requests/${encodeURIComponent(requestId)}/change-requests`, { method: "POST", body: payload, mutation: true });
  }

  async createDeletionRequest(mode: DeletionRequest["mode"]): Promise<DeletionRequest> {
    return this.request<DeletionRequest>("/deletion-requests", { method: "POST", body: { mode }, mutation: true });
  }

  async confirmDeletionRequest(requestId: string): Promise<DeletionRequest> {
    return this.request<DeletionRequest>(`/deletion-requests/${encodeURIComponent(requestId)}/confirm`, { method: "POST", body: {}, mutation: true });
  }

  private async request<T = void>(
    path: string,
    options: { method?: "POST" | "PATCH"; body?: unknown; mutation?: boolean; csrf?: boolean } = {}
  ): Promise<T> {
    const mutation = options.mutation ?? false;
    const headers: Record<string, string> = { Accept: "application/json" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (mutation) {
      headers["X-CSRF-Token"] = this.csrfToken;
      headers["Idempotency-Key"] = crypto.randomUUID();
    }
    const response = await fetch(`${API_BASE}${path}`, {
      method: options.method ?? "GET",
      credentials: "same-origin",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    });
    if (response.status === 204) {
      return undefined as T;
    }
    const body = (await response.json().catch(() => ({}))) as T & ApiErrorPayload;
    if (!response.ok) {
      const error = body as ApiErrorPayload;
      throw new ApiError(response.status, error.error?.code ?? "UNKNOWN", error.error?.message ?? "Не удалось выполнить запрос.", error.error?.retryable ?? false);
    }
    return body;
  }
}

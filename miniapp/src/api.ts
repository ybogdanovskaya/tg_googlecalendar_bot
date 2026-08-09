import type {
  BookingCalendar,
  BookingConfig,
  BookingSlots,
  AdminDashboard,
  AdminStatistics,
  CalendarIntegration,
  AdminChangeRequest,
  AdminSettings,
  ChangeRequest,
  DeletionRequest,
  EventSeries,
  EventOccurrence,
  MeetingRequest,
  RequestAlternative,
  SessionInfo,
  UserRequests
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

  async requests(): Promise<UserRequests> {
    return this.request<UserRequests>("/requests");
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

  async adminDashboard(): Promise<AdminDashboard> {
    return this.request<AdminDashboard>("/admin/dashboard");
  }

  async adminStatistics(fromDate?: string, toDate?: string): Promise<AdminStatistics> {
    const params = new URLSearchParams();
    if (fromDate) params.set("from_date", fromDate);
    if (toDate) params.set("to_date", toDate);
    return this.request<AdminStatistics>(`/admin/statistics${params.size ? `?${params}` : ""}`);
  }

  async adminCalendarIntegration(): Promise<CalendarIntegration> {
    return this.request<CalendarIntegration>("/admin/integration/calendar");
  }

  async adminRequests(): Promise<MeetingRequest[]> {
    const response = await this.request<{ items: MeetingRequest[] }>("/admin/requests");
    return response.items;
  }

  async updateAdminRequest(requestId: string, payload: Partial<Pick<MeetingRequest, "name" | "email" | "subject" | "description" | "location">>): Promise<MeetingRequest> {
    return this.request<MeetingRequest>(`/admin/requests/${encodeURIComponent(requestId)}`, { method: "PATCH", body: payload, mutation: true });
  }

  async adminChangeRequests(): Promise<AdminChangeRequest[]> {
    const response = await this.request<{ items: AdminChangeRequest[] }>("/admin/change-requests");
    return response.items;
  }

  async approveAdminChange(changeId: string): Promise<AdminChangeRequest> {
    return this.request<AdminChangeRequest>(`/admin/change-requests/${encodeURIComponent(changeId)}/approve`, { method: "POST", body: {}, mutation: true });
  }

  async rejectAdminChange(changeId: string): Promise<ChangeRequest> {
    return this.request<ChangeRequest>(`/admin/change-requests/${encodeURIComponent(changeId)}/reject`, { method: "POST", body: {}, mutation: true });
  }

  async approveAdminRequest(requestId: string): Promise<MeetingRequest> {
    return this.request<MeetingRequest>(`/admin/requests/${encodeURIComponent(requestId)}/approve`, { method: "POST", body: {}, mutation: true });
  }

  async rejectAdminRequest(requestId: string): Promise<MeetingRequest> {
    return this.request<MeetingRequest>(`/admin/requests/${encodeURIComponent(requestId)}/reject`, { method: "POST", body: {}, mutation: true });
  }

  async createAdminAlternative(requestId: string, startAt: string, durationMinutes: number): Promise<RequestAlternative> {
    return this.request<RequestAlternative>(`/admin/requests/${encodeURIComponent(requestId)}/alternatives`, { method: "POST", body: { start_at: startAt, duration_minutes: durationMinutes }, mutation: true });
  }

  async createAdminManualMeeting(payload: {
    subject: string;
    email?: string;
    description?: string;
    location?: string;
    start_at: string;
    duration_minutes: number;
    blocks_calendar: boolean;
    allow_overlap: boolean;
  }): Promise<MeetingRequest> {
    return this.request<MeetingRequest>("/admin/manual-meetings", { method: "POST", body: payload, mutation: true });
  }

  async adminManualMeetings(): Promise<MeetingRequest[]> {
    const response = await this.request<{ items: MeetingRequest[] }>("/admin/manual-meetings");
    return response.items;
  }

  async cancelAdminManualMeeting(requestId: string): Promise<MeetingRequest> {
    return this.request<MeetingRequest>(`/admin/manual-meetings/${encodeURIComponent(requestId)}/cancel`, { method: "POST", body: {}, mutation: true });
  }

  async updateAdminManualMeeting(requestId: string, payload: Partial<Pick<MeetingRequest, "name" | "email" | "subject" | "description" | "location">>): Promise<MeetingRequest> {
    return this.request<MeetingRequest>(`/admin/manual-meetings/${encodeURIComponent(requestId)}`, { method: "PATCH", body: payload, mutation: true });
  }

  async adminSeries(): Promise<EventSeries[]> {
    const response = await this.request<{ items: EventSeries[] }>("/admin/series");
    return response.items;
  }

  async createAdminSeries(payload: {
    subject: string;
    email?: string;
    start_at: string;
    duration_minutes: number;
    frequency: EventSeries["frequency"];
    until_date: string;
    blocks_calendar: boolean;
    allow_overlap: boolean;
  }): Promise<EventSeries> {
    return this.request<EventSeries>("/admin/series", { method: "POST", body: payload, mutation: true });
  }

  async cancelAdminSeries(seriesId: string): Promise<EventSeries> {
    return this.request<EventSeries>(`/admin/series/${encodeURIComponent(seriesId)}/cancel`, { method: "POST", body: {}, mutation: true });
  }

  async adminSeriesOccurrences(seriesId: string): Promise<EventOccurrence[]> {
    const response = await this.request<{ items: EventOccurrence[] }>(`/admin/series/${encodeURIComponent(seriesId)}/occurrences`);
    return response.items;
  }

  async cancelAdminOccurrence(seriesId: string, occurrenceId: string): Promise<EventOccurrence> {
    return this.request<EventOccurrence>(`/admin/series/${encodeURIComponent(seriesId)}/occurrences/${encodeURIComponent(occurrenceId)}/cancel`, { method: "POST", body: {}, mutation: true });
  }

  async moveAdminOccurrence(seriesId: string, occurrenceId: string, startAt: string, durationMinutes: number): Promise<EventOccurrence> {
    return this.request<EventOccurrence>(`/admin/series/${encodeURIComponent(seriesId)}/occurrences/${encodeURIComponent(occurrenceId)}`, { method: "PATCH", body: { start_at: startAt, duration_minutes: durationMinutes }, mutation: true });
  }

  async adminSettings(): Promise<AdminSettings> {
    return this.request<AdminSettings>("/admin/settings");
  }

  async updateAdminSetting(key: string, value: unknown): Promise<{ key: string; value: unknown }> {
    return this.request<{ key: string; value: unknown }>("/admin/settings", { method: "PATCH", body: { key, value }, mutation: true });
  }

  async adminClosedDates(): Promise<{ items: string[]; weekdays: number[] }> {
    return this.request<{ items: string[]; weekdays: number[] }>("/admin/closed-dates");
  }

  async addAdminClosedDate(date: string): Promise<void> {
    await this.request("/admin/closed-dates", { method: "POST", body: { date }, mutation: true });
  }

  async removeAdminClosedDate(date: string): Promise<void> {
    await this.request(`/admin/closed-dates/${encodeURIComponent(date)}`, { method: "DELETE", mutation: true });
  }

  private async request<T = void>(
    path: string,
    options: { method?: "POST" | "PATCH" | "DELETE"; body?: unknown; mutation?: boolean; csrf?: boolean } = {}
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

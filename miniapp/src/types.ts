export type Role = "USER" | "ADMIN";

export interface SessionInfo {
  role: Role;
  consent: { accepted: boolean; version: string };
  timezone: string;
  expires_at: string;
}

export interface BookingConfig {
  timezone: string;
  booking_enabled: boolean;
  durations: number[];
  step_minutes: number;
  horizon_days: number;
  min_lead_minutes: number;
  hold_hours: number;
  window: { start_minutes: number; end_minutes: number };
}

export interface BookingCalendar {
  available_dates: string[];
  closed_dates: string[];
}

export interface BookingSlots {
  slots: string[];
  period_counts: Record<string, number>;
}

export interface MeetingRequest {
  id: string;
  subject: string;
  description: string | null;
  location: string | null;
  email: string;
  name: string;
  start_at: string;
  end_at: string;
  duration_minutes: number;
  all_day: boolean;
  blocks_calendar: boolean;
  status: string;
  status_label: string;
  reservation: { active: boolean; until: string };
  allowed_actions: string[];
  open_change: ChangeRequest | null;
  created_at: string;
  updated_at: string;
}

export interface UserRequests {
  items: MeetingRequest[];
  archive: MeetingRequest[];
}

export interface RequestAlternative {
  id: string;
  start_at: string;
  end_at: string;
  duration_minutes: number;
  expires_at: string;
}

export interface ChangeRequest {
  id: string;
  change_type: "CANCEL" | "RESCHEDULE";
  status: string;
  proposed_start_at: string | null;
  proposed_end_at: string | null;
  created_at: string;
}

export interface AdminChangeRequest {
  change: ChangeRequest;
  request: MeetingRequest;
}

export interface DeletionRequest {
  id: string;
  mode: "CANCEL_FUTURE" | "KEEP_FUTURE";
  status: string;
  future_meeting_count: number;
  execute_after: string | null;
}

export interface RequestDraft {
  durationMinutes: number | null;
  date: string | null;
  slot: string | null;
  name: string;
  email: string;
  subject: string;
  description: string;
  location: string;
}

export interface AdminDashboard {
  pending_requests: number;
  pending_changes: number;
  statistics: {
    user_requests: number;
    manual_meetings: number;
    calendar_meetings: number;
    unique_users: number;
  };
}

export interface AdminSettings {
  booking: {
    booking_enabled: boolean;
    min_lead_minutes: number;
    booking_horizon_days: number;
    hold_hours: number;
    durations: number[];
    step_minutes: number;
    user_booking_window: number[];
    closed_weekdays: number[];
  };
  notifications: {
    reminder_minutes: number[];
    pending_reminder_hours: number;
    automation_enabled: boolean;
  };
}

export interface EventSeries {
  id: string;
  subject: string;
  email: string | null;
  description: string | null;
  location: string | null;
  start_at: string;
  end_at: string;
  frequency: "DAILY" | "WEEKLY" | "MONTHLY";
  until_date: string;
  status: string;
  blocks_calendar: boolean;
  allow_overlap: boolean;
}

export interface EventOccurrence {
  id: string;
  series_id: string;
  status: string;
  start_at: string;
  end_at: string;
}

export interface AdminStatistics {
  from_date: string;
  to_date: string;
  user_requests: number;
  manual_meetings: number;
  calendar_meetings: number;
  unique_users: number;
}

export interface CalendarIntegration {
  status: "OK" | "UNAVAILABLE";
  checked_at: string;
}

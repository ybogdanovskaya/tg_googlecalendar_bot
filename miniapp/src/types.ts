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
  status: string;
  status_label: string;
  reservation: { active: boolean; until: string };
  allowed_actions: string[];
  created_at: string;
  updated_at: string;
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

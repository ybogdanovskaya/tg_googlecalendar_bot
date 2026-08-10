export type DayPeriod = "morning" | "day" | "evening";

export function slotsForDayPeriod(slots: string[], period: DayPeriod): string[] {
  return slots.filter((slot) => {
    const hour = Number(slot.slice(11, 13));
    if (period === "morning") return hour < 12;
    if (period === "day") return hour >= 12 && hour < 17;
    return hour >= 17;
  });
}

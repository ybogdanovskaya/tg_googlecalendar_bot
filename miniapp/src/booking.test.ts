import { describe, expect, it } from "vitest";

import { slotsForDayPeriod } from "./booking";

describe("slotsForDayPeriod", () => {
  it("does not show morning slots after selecting the day period", () => {
    const slots = [
      "2026-08-20T08:00:00+03:00",
      "2026-08-20T11:45:00+03:00",
      "2026-08-20T12:00:00+03:00",
      "2026-08-20T16:45:00+03:00",
      "2026-08-20T17:00:00+03:00",
    ];

    expect(slotsForDayPeriod(slots, "day")).toEqual([
      "2026-08-20T12:00:00+03:00",
      "2026-08-20T16:45:00+03:00",
    ]);
  });
});

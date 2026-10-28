import { describe, expect, it } from "vitest";
import { formatAthens, formatNewYork, formatNewYorkPeriod } from "./format";

const SCAN = "2026-10-19T17:33:00+00:00";

describe("market clock labels", () => {
  it("shows the market-data clock in New York and the last SIP-complete window", () => {
    expect(formatNewYork(SCAN)).toBe("19/10/2026, 13:33 EDT");
    expect(formatNewYorkPeriod(SCAN)).toBe("12:30-13:00");
  });

  it("keeps the scan execution clock in Athens", () => {
    expect(formatAthens("2026-10-19T17:41:00+00:00")).toBe("19/10/2026, 20:41 EEST");
  });

  it("reads a 30-minute window only after its own SIP delay", () => {
    expect(formatNewYorkPeriod("2026-10-19T14:14:00+00:00", "30m")).toBe("—");
    expect(formatNewYorkPeriod("2026-10-19T14:15:00+00:00", "30m")).toBe("09:30-10:00");
    expect(formatNewYorkPeriod("2026-10-19T17:33:00+00:00", "30m")).toBe("12:30-13:00");
    expect(formatNewYorkPeriod("2026-10-19T20:14:00+00:00", "30m")).toBe("15:00-15:30");
    expect(formatNewYorkPeriod("2026-10-19T20:15:00+00:00", "30m")).toBe("15:30-16:00");
  });

  it("advances the window only after the 15-minute SIP delay", () => {
    expect(formatNewYorkPeriod("2026-10-19T17:30:00+00:00")).toBe("12:30-13:00");
    expect(formatNewYorkPeriod("2026-10-19T17:45:00+00:00")).toBe("13:00-13:30");
    expect(formatNewYorkPeriod("2026-10-19T20:00:00+00:00")).toBe("15:00-15:30");
    expect(formatNewYorkPeriod("2026-10-19T20:15:00+00:00")).toBe("15:30-16:00");
    expect(formatNewYorkPeriod("2026-10-19T14:14:00+00:00")).toBe("—");
    expect(formatNewYorkPeriod("2026-10-19T14:15:00+00:00")).toBe("09:30-10:00");
  });
});

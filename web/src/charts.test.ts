import { describe, expect, it } from "vitest";
import { chartSegments, plotSessionChart, sessionTimeSlots, type ChartPoint } from "./charts";
import { snapshotRole } from "./format";

function point(at: string, value: number | null, kind: ChartPoint["kind"] = "LOOKBACK"): ChartPoint {
  return { label: at, value, kind, at };
}

describe("charts and snapshot roles", () => {
  it("breaks the line on null values and does not plot them as zero", () => {
    const segments = chartSegments(
      [
        { label: "a", value: 10, kind: "LOOKBACK" },
        { label: "b", value: null, kind: "LOOKBACK" },
        { label: "c", value: 0, kind: "CURRENT" },
      ],
      100,
      40,
    );
    expect(segments).toHaveLength(2);
    expect(segments[0]).toHaveLength(1);
    expect(segments[1][0].value).toBe(0);
    expect(segments.flat().some((point) => point.index === 1)).toBe(false);
  });

  it("keeps the six session hours and draws 15:30-16:00 as a half bar", () => {
    const slots = sessionTimeSlots([
      point("2026-10-15T13:00:00Z", 1),
      point("2026-10-15T13:30:00Z", 2),
      point("2026-10-15T14:30:00Z", 3),
      point("2026-10-15T15:30:00Z", 4),
      point("2026-10-15T16:30:00Z", 5),
      point("2026-10-15T17:30:00Z", 6),
      point("2026-10-15T18:30:00Z", 7),
      point("2026-10-15T19:30:00Z", 8),
    ]);
    expect(slots.map((slot) => slot.axisLabel)).toEqual(["9:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]);
    expect(slots.map((slot) => slot.weight)).toEqual([1, 1, 1, 1, 1, 1, 0.5]);
    expect(slots.map((slot) => slot.value)).toEqual([2, 3, 4, 5, 6, 7, 8]);
    const layout = plotSessionChart(slots, 400, 200, 1);
    expect(layout?.slotSpans[6].width).toBeCloseTo((layout?.slotSpans[0].width ?? 0) / 2);
  });

  it("omits overnight and weekend hours", () => {
    const slots = sessionTimeSlots([
      point("2026-10-16T20:00:00Z", 1),
      point("2026-10-19T13:30:00Z", 2),
    ]);
    expect(slots.map((slot) => `${slot.dateLabel} ${slot.axisLabel}`)).toEqual(["16/10 15:30", "19/10 9:30"]);
  });

  it("leaves a gap for a missing session window and scales y to the values", () => {
    const slots = sessionTimeSlots([
      point("2026-10-15T13:30:00Z", 60),
      point("2026-10-15T15:30:00Z", 70),
    ]);
    expect(slots.map((slot) => [slot.axisLabel, slot.value])).toEqual([
      ["9:30", 60],
      ["10:30", null],
      ["11:30", 70],
    ]);
    const layout = plotSessionChart(slots, 400, 200, 1);
    expect(layout?.segments).toHaveLength(2);
    expect(layout?.yMin).toBe(60);
    expect(layout?.yMax).toBe(70);
    const plotted = layout?.segments.flat() ?? [];
    expect(plotted.find((item) => item.value === 60)?.y).toBeCloseTo(layout?.plot.bottom ?? 0);
    expect(plotted.find((item) => item.value === 70)?.y).toBeCloseTo(layout?.plot.top ?? 0);
  });

  it("does not connect Friday's close to Monday's open", () => {
    const slots = sessionTimeSlots([
      point("2026-10-16T20:00:00Z", 10),
      point("2026-10-19T13:30:00Z", 20),
    ]);
    expect(plotSessionChart(slots, 400, 200, 1)?.segments).toHaveLength(2);
  });

  it("keeps the print nearest the window start when two snapshots share it", () => {
    const slots = sessionTimeSlots([
      point("2026-10-19T13:30:00Z", 40, "LOOKBACK"),
      point("2026-10-19T14:00:00Z", 49, "CURRENT"),
    ]);
    expect(slots).toHaveLength(1);
    expect(slots[0].axisLabel).toBe("9:30");
    expect(slots[0].clock).toBe("09:30");
    expect(slots[0].value).toBe(40);
    expect(slots[0].kind).toBe("LOOKBACK");
  });

  it("does not treat LOOKBACK as the operational decision", () => {
    expect(snapshotRole("CURRENT")).toBe("decision");
    expect(snapshotRole("LOOKBACK")).toBe("lookback");
    expect(snapshotRole(null)).toBe("legacy");
  });
});

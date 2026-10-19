import { describe, expect, it } from "vitest";
import { chartSegments } from "./charts";
import { snapshotRole } from "./format";

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

  it("does not treat LOOKBACK as the operational decision", () => {
    expect(snapshotRole("CURRENT")).toBe("decision");
    expect(snapshotRole("LOOKBACK")).toBe("lookback");
    expect(snapshotRole(null)).toBe("legacy");
  });
});

import { describe, expect, it } from "vitest";
import type { ScanRow } from "./api/types";
import { nextSort, sortScanRows } from "./sort";

function row(partial: Partial<ScanRow> & Pick<ScanRow, "symbol">): ScanRow {
  return {
    rank: null,
    price: null,
    status: "BLOCKED",
    score: null,
    movement: null,
    movement_label: null,
    previous_rank: null,
    rsi14: null,
    macd_hist: null,
    atr_percent: null,
    volume_ratio: null,
    adv20: null,
    veto_reasons: [],
    ...partial,
  };
}

describe("dashboard column sort", () => {
  const rows = [
    row({ symbol: "AAPL", rank: 2, score: 7.1, rsi14: 40 }),
    row({ symbol: "MSFT", rank: 1, score: null, rsi14: 70 }),
    row({ symbol: "NVDA", rank: 3, score: 8.4, rsi14: 55 }),
  ];

  it("starts numeric columns high to low and text columns A to Z", () => {
    expect(nextSort(null, "score")).toEqual({ key: "score", direction: "desc" });
    expect(nextSort(null, "symbol")).toEqual({ key: "symbol", direction: "asc" });
  });

  it("toggles the same column between descending and ascending", () => {
    const first = nextSort(null, "rsi14");
    expect(nextSort(first, "rsi14").direction).toBe("asc");
    expect(nextSort(nextSort(first, "rsi14"), "rsi14").direction).toBe("desc");
  });

  it("sorts scores high to low and keeps missing scores last", () => {
    const sorted = sortScanRows(rows, { key: "score", direction: "desc" });
    expect(sorted.map((item) => item.symbol)).toEqual(["NVDA", "AAPL", "MSFT"]);
  });

  it("sorts scores low to high and still keeps missing scores last", () => {
    const sorted = sortScanRows(rows, { key: "score", direction: "asc" });
    expect(sorted.map((item) => item.symbol)).toEqual(["AAPL", "NVDA", "MSFT"]);
  });
});

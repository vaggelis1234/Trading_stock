import { describe, expect, it } from "vitest";
import { parseLatestScan, parseSymbolSearch, parseSymbolView, ResponseShapeError } from "./parse";

const row = {
  rank: 1,
  symbol: "AAPL",
  price: 100,
  status: "PASS",
  score: 6,
  movement: -3,
  movement_label: "down",
  previous_rank: 5,
  rsi14: null,
  macd_hist: 0.1,
  atr_percent: 1.8,
  volume_ratio: 1.1,
  adv20: 8000000,
  veto_reasons: [],
};

describe("API response parsing", () => {
  it("accepts the latest scan contract, including null indicators", () => {
    const scan = parseLatestScan({
      scan_timestamp: "2026-10-09T17:02:00+00:00",
      executed_at: null,
      run_id: "run-a",
      snapshot_kind: "CURRENT",
      evaluated: 2,
      scored: 1,
      passed: 1,
      blocked: 1,
      earnings_pending: true,
      rows: [row, { ...row, symbol: "MSFT", rank: null, score: null, movement: null, movement_label: null, previous_rank: null, status: "BLOCKED", price: null, atr_percent: null }],
    });
    expect(scan.rows[1].score).toBeNull();
    expect(scan.rows[1].atr_percent).toBeNull();
    expect(scan.snapshot_kind).toBe("CURRENT");
  });

  it("rejects a response that treats an unknown kind as a scan", () => {
    expect(() =>
      parseLatestScan({
        scan_timestamp: "2026-10-09T17:02:00+00:00",
        executed_at: null,
        run_id: null,
        snapshot_kind: "MAYBE",
        evaluated: 0,
        scored: 0,
        passed: 0,
        blocked: 0,
        earnings_pending: false,
        rows: [],
      }),
    ).toThrow(ResponseShapeError);
  });

  it("parses search pagination without history fields", () => {
    const body = parseSymbolSearch({
      scan_timestamp: "2026-10-09T17:02:00+00:00",
      run_id: null,
      snapshot_kind: null,
      total: 120,
      limit: 25,
      offset: 25,
      rows: [row],
    });
    expect(body.total).toBe(120);
    expect(body.rows).toHaveLength(1);
    expect(body).not.toHaveProperty("history");
  });

  it("keeps LOOKBACK history distinct from the operational CURRENT scan", () => {
    const view = parseSymbolView({
      symbol: "AAPL",
      scan_timestamp: "2026-10-09T17:02:00+00:00",
      executed_at: "2026-10-09T17:02:10+00:00",
      run_id: "run-a",
      snapshot_kind: "CURRENT",
      range: "24H",
      since: "2026-10-08T17:00:00+00:00",
      detail: {
        rank: 1,
        movement: null,
        movement_label: "new",
        previous_rank: null,
        score: 6,
        status: "PASS",
        reasons: [],
        veto_reasons: [],
        price: 100,
        rsi14: 50,
        macd_hist: null,
        atr_percent: null,
        volume_ratio: 1.1,
        adv20: null,
        earnings_check: "unavailable",
      },
      enrichment: null,
      history: [
        {
          scan_timestamp: "2026-10-09T16:00:00+00:00",
          run_id: "run-a",
          snapshot_kind: "LOOKBACK",
          status: "BLOCKED",
          reasons: ["VolumeRatio < 0.8"],
          veto_reasons: ["BLOCKED_VOLUME"],
          rsi14: null,
          final_score: 4.5,
          atr_percent: null,
          volume_ratio: 0.5,
        },
      ],
      coverage: {
        RSI: { expected: ["2026-10-09T17:00:00+00:00"], actual: ["2026-10-09T17:02:00+00:00"], missing: [], null_metric: [] },
        Score: { expected: [], actual: [], missing: [], null_metric: [] },
        "ATR%": { expected: [], actual: [], missing: [], null_metric: ["2026-10-09T16:00:00+00:00"] },
        "Volume Ratio": { expected: [], actual: [], missing: [], null_metric: [] },
      },
    });
    expect(view.snapshot_kind).toBe("CURRENT");
    expect(view.history[0].snapshot_kind).toBe("LOOKBACK");
    expect(view.detail.atr_percent).toBeNull();
    expect(view.enrichment).toBeNull();
  });
});

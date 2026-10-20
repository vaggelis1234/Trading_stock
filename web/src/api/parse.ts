import type {
  CoverageMap,
  HistoryPoint,
  HistoryRange,
  LatestScan,
  MetricCoverage,
  MovementLabel,
  ScanRow,
  ScanStatus,
  SnapshotKind,
  SymbolDetail,
  SymbolSearch,
  SymbolView,
  RulesCatalog,
} from "./types";

export class ResponseShapeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ResponseShapeError";
  }
}

const COVERAGE_NAMES = ["RSI", "Score", "ATR%", "Volume Ratio"] as const;

export function parseRules(value: unknown): RulesCatalog {
  const body = record(value, "rules");
  return {
    groups: array(body, "groups").map((item) => {
      const group = record(item, "rule group");
      return {
        title: text(group, "title"),
        note: text(group, "note"),
        rows: array(group, "rows").map((row) => {
          const rule = record(row, "rule");
          return { name: text(rule, "name"), value: text(rule, "value") };
        }),
      };
    }),
  };
}

export function parseLatestScan(value: unknown): LatestScan {
  const body = record(value, "latest scan");
  return {
    scan_timestamp: text(body, "scan_timestamp"),
    executed_at: textOrNull(body, "executed_at"),
    run_id: textOrNull(body, "run_id"),
    snapshot_kind: kind(body.snapshot_kind),
    evaluated: integer(body, "evaluated"),
    scored: integer(body, "scored"),
    passed: integer(body, "passed"),
    blocked: integer(body, "blocked"),
    earnings_pending: bool(body, "earnings_pending"),
    rows: array(body, "rows").map(parseScanRow),
  };
}

export function parseSymbolSearch(value: unknown): SymbolSearch {
  const body = record(value, "symbol search");
  return {
    scan_timestamp: text(body, "scan_timestamp"),
    run_id: textOrNull(body, "run_id"),
    snapshot_kind: kind(body.snapshot_kind),
    total: integer(body, "total"),
    limit: integer(body, "limit"),
    offset: integer(body, "offset"),
    rows: array(body, "rows").map(parseScanRow),
  };
}

export function parseSymbolView(value: unknown): SymbolView {
  const body = record(value, "symbol");
  const range = text(body, "range");
  if (range !== "24H" && range !== "3D" && range !== "5D") {
    throw new ResponseShapeError("range");
  }
  return {
    symbol: text(body, "symbol"),
    name: textOrNull(body, "name"),
    scan_timestamp: text(body, "scan_timestamp"),
    executed_at: textOrNull(body, "executed_at"),
    run_id: textOrNull(body, "run_id"),
    snapshot_kind: kind(body.snapshot_kind),
    range,
    since: text(body, "since"),
    detail: parseDetail(body.detail),
    enrichment: enrichment(body.enrichment),
    history: array(body, "history").map(parseHistory),
    coverage: parseCoverage(body.coverage),
  };
}

export function parseScanRow(value: unknown): ScanRow {
  const body = record(value, "row");
  return {
    rank: integerOrNull(body, "rank"),
    symbol: text(body, "symbol"),
    price: numberOrNull(body, "price"),
    status: status(body.status),
    score: numberOrNull(body, "score"),
    movement: integerOrNull(body, "movement"),
    movement_label: movement(body.movement_label),
    previous_rank: integerOrNull(body, "previous_rank"),
    rsi14: numberOrNull(body, "rsi14"),
    macd_hist: numberOrNull(body, "macd_hist"),
    atr_percent: numberOrNull(body, "atr_percent"),
    volume_ratio: numberOrNull(body, "volume_ratio"),
    adv20: numberOrNull(body, "adv20"),
    veto_reasons: stringList(body, "veto_reasons"),
  };
}

function parseDetail(value: unknown): SymbolDetail {
  const body = record(value, "detail");
  return {
    rank: integerOrNull(body, "rank"),
    movement: integerOrNull(body, "movement"),
    movement_label: movement(body.movement_label),
    previous_rank: integerOrNull(body, "previous_rank"),
    score: numberOrNull(body, "score"),
    status: status(body.status),
    reasons: stringList(body, "reasons"),
    veto_reasons: stringList(body, "veto_reasons"),
    price: numberOrNull(body, "price"),
    rsi14: numberOrNull(body, "rsi14"),
    macd_hist: numberOrNull(body, "macd_hist"),
    atr_percent: numberOrNull(body, "atr_percent"),
    volume_ratio: numberOrNull(body, "volume_ratio"),
    adv20: numberOrNull(body, "adv20"),
    earnings_check: textOrNull(body, "earnings_check"),
  };
}

function parseHistory(value: unknown): HistoryPoint {
  const body = record(value, "history");
  return {
    scan_timestamp: text(body, "scan_timestamp"),
    run_id: textOrNull(body, "run_id"),
    snapshot_kind: kind(body.snapshot_kind),
    status: status(body.status),
    reasons: stringList(body, "reasons"),
    veto_reasons: stringList(body, "veto_reasons"),
    price: numberOrNull(body, "price"),
    rsi14: numberOrNull(body, "rsi14"),
    final_score: numberOrNull(body, "final_score"),
    atr_percent: numberOrNull(body, "atr_percent"),
    volume_ratio: numberOrNull(body, "volume_ratio"),
  };
}

function parseCoverage(value: unknown): CoverageMap {
  const body = record(value, "coverage");
  const coverage = {} as CoverageMap;
  for (const name of COVERAGE_NAMES) {
    coverage[name] = parseMetricCoverage(body[name]);
  }
  return coverage;
}

function parseMetricCoverage(value: unknown): MetricCoverage {
  const body = record(value, "coverage metric");
  return {
    expected: stringList(body, "expected"),
    actual: stringList(body, "actual"),
    missing: stringList(body, "missing"),
    null_metric: stringList(body, "null_metric"),
  };
}

function enrichment(value: unknown): Record<string, string | number | null> | null {
  if (value === null) return null;
  const body = record(value, "enrichment");
  const parsed: Record<string, string | number | null> = {};
  for (const [key, item] of Object.entries(body)) {
    if (item === null || typeof item === "string" || typeof item === "number") {
      parsed[key] = item;
    } else {
      throw new ResponseShapeError(`enrichment.${key}`);
    }
  }
  return parsed;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ResponseShapeError(label);
  }
  return value as Record<string, unknown>;
}

function array(body: Record<string, unknown>, key: string): unknown[] {
  const value = body[key];
  if (!Array.isArray(value)) throw new ResponseShapeError(key);
  return value;
}

function text(body: Record<string, unknown>, key: string): string {
  const value = body[key];
  if (typeof value !== "string") throw new ResponseShapeError(key);
  return value;
}

function textOrNull(body: Record<string, unknown>, key: string): string | null {
  const value = body[key];
  if (value === null) return null;
  if (typeof value !== "string") throw new ResponseShapeError(key);
  return value;
}

function bool(body: Record<string, unknown>, key: string): boolean {
  const value = body[key];
  if (typeof value !== "boolean") throw new ResponseShapeError(key);
  return value;
}

function integer(body: Record<string, unknown>, key: string): number {
  const value = numberOrNull(body, key);
  if (value === null || !Number.isInteger(value)) throw new ResponseShapeError(key);
  return value;
}

function integerOrNull(body: Record<string, unknown>, key: string): number | null {
  const value = numberOrNull(body, key);
  if (value !== null && !Number.isInteger(value)) throw new ResponseShapeError(key);
  return value;
}

function numberOrNull(body: Record<string, unknown>, key: string): number | null {
  const value = body[key];
  if (value === null) return null;
  if (typeof value !== "number" || Number.isNaN(value)) throw new ResponseShapeError(key);
  return value;
}

function stringList(body: Record<string, unknown>, key: string): string[] {
  const value = body[key];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new ResponseShapeError(key);
  }
  return value;
}

function status(value: unknown): ScanStatus {
  if (value === "PASS" || value === "BLOCKED") return value;
  throw new ResponseShapeError("status");
}

function movement(value: unknown): MovementLabel {
  if (value === null || value === "up" || value === "down" || value === "flat" || value === "new") {
    return value;
  }
  throw new ResponseShapeError("movement_label");
}

function kind(value: unknown): SnapshotKind {
  if (value === null || value === "CURRENT" || value === "LOOKBACK") return value;
  throw new ResponseShapeError("snapshot_kind");
}

export function isHistoryRange(value: string): value is HistoryRange {
  return value === "24H" || value === "3D" || value === "5D";
}

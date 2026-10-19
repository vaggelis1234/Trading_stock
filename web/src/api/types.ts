/** Shapes returned by src/ui/api/read_models.py. */

export type SnapshotKind = "CURRENT" | "LOOKBACK" | null;
export type ScanStatus = "PASS" | "BLOCKED";
export type MovementLabel = "up" | "down" | "flat" | "new" | null;
export type HistoryRange = "24H" | "3D" | "5D";

export interface ScanRow {
  rank: number | null;
  symbol: string;
  price: number | null;
  status: ScanStatus;
  score: number | null;
  movement: number | null;
  movement_label: MovementLabel;
  previous_rank: number | null;
  rsi14: number | null;
  macd_hist: number | null;
  atr_percent: number | null;
  volume_ratio: number | null;
  adv20: number | null;
  veto_reasons: string[];
}

export interface RuleRow {
  name: string;
  value: string;
}

export interface RuleGroup {
  title: string;
  note: string;
  rows: RuleRow[];
}

export interface RulesCatalog {
  groups: RuleGroup[];
}

export interface LatestScan {
  scan_timestamp: string;
  executed_at: string | null;
  run_id: string | null;
  snapshot_kind: SnapshotKind;
  evaluated: number;
  scored: number;
  passed: number;
  blocked: number;
  earnings_pending: boolean;
  rows: ScanRow[];
}

export interface SymbolSearch {
  scan_timestamp: string;
  run_id: string | null;
  snapshot_kind: SnapshotKind;
  total: number;
  limit: number;
  offset: number;
  rows: ScanRow[];
}

export interface SymbolDetail {
  rank: number | null;
  movement: number | null;
  movement_label: MovementLabel;
  previous_rank: number | null;
  score: number | null;
  status: ScanStatus;
  reasons: string[];
  veto_reasons: string[];
  price: number | null;
  rsi14: number | null;
  macd_hist: number | null;
  atr_percent: number | null;
  volume_ratio: number | null;
  adv20: number | null;
  earnings_check: string | null;
}

export interface HistoryPoint {
  scan_timestamp: string;
  run_id: string | null;
  snapshot_kind: SnapshotKind;
  status: ScanStatus;
  reasons: string[];
  veto_reasons: string[];
  rsi14: number | null;
  final_score: number | null;
  atr_percent: number | null;
  volume_ratio: number | null;
}

export interface MetricCoverage {
  expected: string[];
  actual: string[];
  missing: string[];
  null_metric: string[];
}

export type CoverageMap = Record<"RSI" | "Score" | "ATR%" | "Volume Ratio", MetricCoverage>;

export interface SymbolView {
  symbol: string;
  scan_timestamp: string;
  executed_at: string | null;
  run_id: string | null;
  snapshot_kind: SnapshotKind;
  range: HistoryRange;
  since: string;
  detail: SymbolDetail;
  enrichment: Record<string, string | number | null> | null;
  history: HistoryPoint[];
  coverage: CoverageMap;
}

export interface ApiFailure {
  status: number;
  code: string | null;
}

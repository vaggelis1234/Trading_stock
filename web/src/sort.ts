import type { ScanRow } from "./api/types";

export type SortKey =
  | "rank"
  | "movement"
  | "symbol"
  | "price"
  | "status"
  | "score"
  | "rsi14"
  | "macd_hist"
  | "atr_percent"
  | "volume_ratio"
  | "adv20";

export type SortDirection = "asc" | "desc";

export interface ColumnSort {
  key: SortKey;
  direction: SortDirection;
}

const TEXT_KEYS = new Set<SortKey>(["symbol", "status"]);

export function nextSort(current: ColumnSort | null, key: SortKey): ColumnSort {
  if (current?.key !== key) {
    return { key, direction: TEXT_KEYS.has(key) ? "asc" : "desc" };
  }
  return { key, direction: current.direction === "desc" ? "asc" : "desc" };
}

export function sortScanRows(rows: ScanRow[], sort: ColumnSort | null): ScanRow[] {
  if (!sort) return rows;
  return rows
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      const compared = compare(left.row, right.row, sort);
      if (compared !== 0) return compared;
      return left.index - right.index;
    })
    .map((item) => item.row);
}

function compare(left: ScanRow, right: ScanRow, sort: ColumnSort): number {
  const a = valueOf(left, sort.key);
  const b = valueOf(right, sort.key);
  if (a === null || b === null) {
    if (a === null && b === null) return 0;
    return a === null ? 1 : -1;
  }
  const compared = typeof a === "string" ? a.localeCompare(b) : a - b;
  return sort.direction === "asc" ? compared : -compared;
}

function valueOf(row: ScanRow, key: SortKey): number | string | null {
  switch (key) {
    case "rank":
      return row.rank;
    case "movement":
      return row.movement;
    case "symbol":
      return row.symbol;
    case "price":
      return row.price;
    case "status":
      return row.status;
    case "score":
      return row.score;
    case "rsi14":
      return row.rsi14;
    case "macd_hist":
      return row.macd_hist;
    case "atr_percent":
      return row.atr_percent;
    case "volume_ratio":
      return row.volume_ratio;
    case "adv20":
      return row.adv20;
  }
}

import { useState } from "react";
import type { ScanRow } from "../api/types";
import { formatAdv, formatNumber, movementText } from "../format";
import { nextSort, sortScanRows, type SortKey } from "../sort";

interface ScanTableProps {
  rows: ScanRow[];
  onOpen: (symbol: string) => void;
}

const COLUMNS: ReadonlyArray<{ key: SortKey; label: string }> = [
  { key: "rank", label: "Rank" },
  { key: "movement", label: "Movement" },
  { key: "symbol", label: "Symbol" },
  { key: "price", label: "Price" },
  { key: "status", label: "Status" },
  { key: "score", label: "Score" },
  { key: "rsi14", label: "RSI" },
  { key: "macd_hist", label: "MACD" },
  { key: "atr_percent", label: "ATR%" },
  { key: "volume_ratio", label: "Volume Ratio" },
  { key: "adv20", label: "ADV" },
];

export function ScanTable({ rows, onOpen }: ScanTableProps) {
  const [sort, setSort] = useState<{ key: SortKey; direction: "asc" | "desc" } | null>(null);
  const ordered = sortScanRows(rows, sort);

  return (
    <div className="table-wrap">
      <table className="scan-table">
        <thead>
          <tr>
            {COLUMNS.map((column) => {
              const active = sort?.key === column.key;
              const direction = active ? sort.direction : undefined;
              return (
                <th key={column.key} aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}>
                  <button
                    type="button"
                    className={active ? "sort-button active" : "sort-button"}
                    onClick={() => setSort((current) => nextSort(current, column.key))}
                  >
                    {column.label}
                    <span aria-hidden="true">{direction === "asc" ? "↑" : direction === "desc" ? "↓" : ""}</span>
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {ordered.map((row) => (
            <tr key={`${row.symbol}-${row.rank ?? "na"}`}>
              <td className="num">{row.rank ?? "—"}</td>
              <td className={movementClass(row.movement_label)}>{movementText(row.movement_label, row.movement)}</td>
              <td>
                <a
                  className="symbol-link"
                  href={`/symbols/${encodeURIComponent(row.symbol)}`}
                  onClick={(event) => {
                    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
                    event.preventDefault();
                    onOpen(row.symbol);
                  }}
                >
                  {row.symbol}
                </a>
              </td>
              <td className="num">{formatNumber(row.price, 2)}</td>
              <td>
                <span className={row.status === "PASS" ? "pill pass" : "pill blocked"}>{row.status}</span>
              </td>
              <td className="num">{formatNumber(row.score, 2)}</td>
              <td className="num">{formatNumber(row.rsi14, 1)}</td>
              <td className="num">{formatNumber(row.macd_hist, 4)}</td>
              <td className="num">{formatNumber(row.atr_percent, 2)}</td>
              <td className="num">{formatNumber(row.volume_ratio, 2)}</td>
              <td className="num">{formatAdv(row.adv20)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function movementClass(label: ScanRow["movement_label"]): string {
  if (label === "up") return "move up";
  if (label === "down") return "move down";
  if (label === "new") return "move fresh";
  return "move";
}

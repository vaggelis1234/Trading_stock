import { useEffect, useState } from "react";
import { fetchSymbol } from "../api/client";
import type { HistoryRange, SymbolView } from "../api/types";
import { sessionWindowLabel, type ChartPoint } from "../charts";
import { MetricChart } from "../components/MetricChart";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { formatAdv, formatAthens, formatNewYork, formatNumber, movementText, snapshotLabel, snapshotRole } from "../format";

const RANGES: HistoryRange[] = ["24H", "3D", "5D"];

export function SymbolPage({ symbol }: { symbol: string }) {
  const [range, setRange] = useState<HistoryRange>("24H");
  const [view, setView] = useState<SymbolView | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchSymbol(symbol, range)
      .then((body) => {
        if (!active) return;
        setView(body);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setView(null);
        setError(reason);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [symbol, range]);

  if (loading) return <LoadingState label="Φόρτωση μετοχής…" />;
  if (error) return <ErrorState error={error} />;
  if (!view) return <EmptyState title="Δεν υπάρχουν δεδομένα για αυτό το σύμβολο." />;

  const detail = view.detail;
  const charts: Array<{ title: string; field: "rsi14" | "final_score" | "atr_percent" | "volume_ratio" | "price"; digits: number }> = [
    { title: "RSI", field: "rsi14", digits: 1 },
    { title: "Score", field: "final_score", digits: 2 },
    { title: "ATR%", field: "atr_percent", digits: 2 },
    { title: "Volume Ratio", field: "volume_ratio", digits: 2 },
    { title: "Price", field: "price", digits: 2 },
  ];

  return (
    <section>
      <header className="page-head">
        <div>
          <h1>
            {view.symbol}
            {view.name ? <span className="company-name"> ({view.name})</span> : null}
          </h1>
          <p className="muted">
            Operational scan {formatAthens(view.scan_timestamp)} · Executed {formatAthens(view.executed_at)} ·{" "}
            {snapshotRole(view.snapshot_kind) === "legacy" ? "Legacy scan, no run identity" : "CURRENT decision"}
          </p>
        </div>
        <span className={detail.status === "PASS" ? "pill pass" : "pill blocked"}>{detail.status}</span>
      </header>
      <p className="banner">
        Μόνο αυτό το scan είναι operational. Οι γραμμές LOOKBACK στο ιστορικό είναι αποθηκευμένες ώρες, όχι ξεχωριστές
        αποφάσεις.
      </p>
      <div className="detail-grid">
        <Fact label="Rank" value={detail.rank === null ? "—" : String(detail.rank)} />
        <Fact label="Previous rank" value={detail.previous_rank === null ? "—" : String(detail.previous_rank)} />
        <Fact label="Movement" value={movementText(detail.movement_label, detail.movement)} />
        <Fact label="Score" value={formatNumber(detail.score, 2)} />
        <Fact label="Price" value={formatNumber(detail.price, 2)} />
        <Fact label="RSI" value={formatNumber(detail.rsi14, 1)} />
        <Fact label="MACD Histogram" value={formatNumber(detail.macd_hist, 4)} />
        <Fact label="ATR%" value={formatNumber(detail.atr_percent, 2)} />
        <Fact label="Volume Ratio" value={formatNumber(detail.volume_ratio, 2)} />
        <Fact label="ADV" value={formatAdv(detail.adv20)} />
      </div>
      <section className="panel">
        <h2>Veto reasons</h2>
        {detail.reasons.length === 0 ? <p className="muted">—</p> : (
          <ul>
            {detail.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        )}
        {detail.veto_reasons.length > 0 ? <p className="muted">Codes: {detail.veto_reasons.join(", ")}</p> : null}
      </section>
      <div className="range-row" role="group" aria-label="History range">
        {RANGES.map((item) => (
          <button key={item} type="button" className={item === range ? "active" : ""} onClick={() => setRange(item)}>
            {item}
          </button>
        ))}
      </div>
      <div className="chart-grid">
        {charts.map((chart) => (
          <MetricChart key={chart.title} title={chart.title} points={toPoints(view, chart.field)} digits={chart.digits} />
        ))}
      </div>
      <section className="panel">
        <h2>Coverage</h2>
        <div className="coverage-grid">
          {(Object.keys(view.coverage) as Array<keyof SymbolView["coverage"]>).map((name) => {
            const item = view.coverage[name];
            return (
              <article key={name}>
                <h3>{name}</h3>
                <p>
                  Session bars {item.actual.length}/{item.expected.length} · missing {item.missing.length} · null{" "}
                  {item.null_metric.length}
                </p>
                {item.missing.length > 0 || item.null_metric.length > 0 ? (
                  <details>
                    <summary>Gaps</summary>
                    {item.missing.length > 0 ? <p className="muted">Missing: {item.missing.map(sessionWindowLabel).join(", ")}</p> : null}
                    {item.null_metric.length > 0 ? <p className="muted">Null: {item.null_metric.map(sessionWindowLabel).join(", ")}</p> : null}
                  </details>
                ) : null}
              </article>
            );
          })}
        </div>
      </section>
      <section className="panel">
        <h2>History</h2>
        <div className="table-wrap">
          <table className="scan-table">
            <thead>
              <tr>
                <th>Time (New York)</th>
                <th>Kind</th>
                <th>Status</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {view.history.map((row) => (
                <tr key={row.scan_timestamp} className={snapshotRole(row.snapshot_kind)}>
                  <td>{formatNewYork(row.scan_timestamp)}</td>
                  <td>{snapshotLabel(row.snapshot_kind)}</td>
                  <td>{row.status}</td>
                  <td>{row.reasons.length ? row.reasons.join("; ") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="panel">
        <h2>Minute enrichment</h2>
        {view.enrichment === null ? (
          <p className="muted">Δεν υπάρχει minute enrichment για αυτό το operational scan.</p>
        ) : (
          <dl className="facts">
            {Object.entries(view.enrichment).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{value === null || value === "" ? "—" : String(value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>
    </section>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <article className="counter">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function toPoints(view: SymbolView, field: "rsi14" | "final_score" | "atr_percent" | "volume_ratio" | "price"): ChartPoint[] {
  return view.history.map((row) => ({
    label: formatAthens(row.scan_timestamp),
    value: row[field],
    kind: row.snapshot_kind,
    at: row.scan_timestamp,
  }));
}

import { useEffect, useState } from "react";
import { fetchLatestScan } from "../api/client";
import type { LatestScan } from "../api/types";
import { ScanTable } from "../components/ScanTable";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { formatAthens, formatNewYork, formatNewYorkPeriod, snapshotLabel } from "../format";

export function Dashboard({ onOpen }: { onOpen: (symbol: string) => void }) {
  const [scan, setScan] = useState<LatestScan | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchLatestScan()
      .then((body) => {
        if (!active) return;
        setScan(body);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setScan(null);
        setError(reason);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  if (loading) return <LoadingState label="Φόρτωση scan…" />;
  if (error) return <ErrorState error={error} />;
  if (!scan) return <EmptyState title="Δεν υπάρχουν snapshots ακόμα." />;

  return (
    <section>
      <header className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="muted">
            {`Market data ${formatNewYork(scan.scan_timestamp)} · Χρονική περίοδος (${formatNewYorkPeriod(scan.scan_timestamp)}) · Scan executed ${formatAthens(scan.executed_at)}`}
          </p>
        </div>
        <span className="pill">{snapshotLabel(scan.snapshot_kind)}</span>
      </header>
      {scan.earnings_pending ? (
        <p className="banner">Το earnings veto δεν εφαρμόζεται. Ο έλεγχος ημερομηνιών είναι unavailable.</p>
      ) : null}
      <div className="counters">
        <Counter label="Evaluated" value={scan.evaluated} />
        <Counter label="Scored" value={scan.scored} />
        <Counter label="PASS" value={scan.passed} />
        <Counter label="BLOCKED" value={scan.blocked} />
      </div>
      {scan.rows.length === 0 ? (
        <EmptyState title="Δεν υπάρχουν ranked μετοχές σε αυτό το scan." />
      ) : (
        <ScanTable rows={scan.rows} onOpen={onOpen} />
      )}
    </section>
  );
}

function Counter({ label, value }: { label: string; value: number }) {
  return (
    <article className="counter">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

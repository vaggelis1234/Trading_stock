import { useEffect, useState } from "react";
import { fetchRules } from "../api/client";
import type { RulesCatalog } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/States";

export function Rules() {
  const [catalog, setCatalog] = useState<RulesCatalog | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchRules()
      .then((body) => {
        if (!active) return;
        setCatalog(body);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setCatalog(null);
        setError(reason);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  if (loading) return <LoadingState label="Φόρτωση rules…" />;
  if (error) return <ErrorState error={error} />;
  if (!catalog || catalog.groups.length === 0) return <EmptyState title="Δεν υπάρχουν rules." />;

  return (
    <section>
      <header className="page-head">
        <div>
          <h1>Rules</h1>
          <p className="muted">Οι σταθερές που χρησιμοποιεί το universe scan.</p>
        </div>
      </header>
      {catalog.groups.map((group) => (
        <section key={group.title} className="rule-group">
          <h2>{group.title}</h2>
          <p className="muted">{group.note}</p>
          <div className="table-wrap">
            <table className="scan-table">
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {group.rows.map((row) => (
                  <tr key={row.name}>
                    <td>{row.name}</td>
                    <td>{row.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </section>
  );
}

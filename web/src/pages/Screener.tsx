import { useEffect, useState } from "react";
import { fetchSymbols } from "../api/client";
import type { SymbolSearch } from "../api/types";
import { ScanTable } from "../components/ScanTable";
import { EmptyState, ErrorState, LoadingState } from "../components/States";

const PAGE_SIZE = 25;

interface Filters {
  q: string;
  status: "" | "PASS" | "BLOCKED";
  minScore: string;
}

const EMPTY: Filters = { q: "", status: "", minScore: "" };

export function Screener({ onOpen }: { onOpen: (symbol: string) => void }) {
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<SymbolSearch | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchSymbols({ ...applied, limit: PAGE_SIZE, offset })
      .then((body) => {
        if (!active) return;
        setResult(body);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setResult(null);
        setError(reason);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [applied, offset]);

  const total = result?.total ?? 0;
  const from = total === 0 ? 0 : offset + 1;
  const to = offset + (result?.rows.length ?? 0);

  return (
    <section>
      <header className="page-head">
        <div>
          <h1>Screener</h1>
          <p className="muted">Αναζήτηση στο τελευταίο operational scan. Δεν φορτώνεται ιστορικό.</p>
        </div>
      </header>
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          setApplied(draft);
        }}
      >
        <label>
          Ticker
          <input
            value={draft.q}
            maxLength={32}
            onChange={(event) => setDraft({ ...draft, q: event.target.value })}
            placeholder="AAPL"
          />
        </label>
        <label>
          Status
          <select
            value={draft.status}
            onChange={(event) =>
              setDraft({ ...draft, status: event.target.value as Filters["status"] })
            }
          >
            <option value="">All</option>
            <option value="PASS">PASS</option>
            <option value="BLOCKED">BLOCKED</option>
          </select>
        </label>
        <label>
          Min score
          <input
            inputMode="decimal"
            value={draft.minScore}
            onChange={(event) => setDraft({ ...draft, minScore: event.target.value })}
            placeholder="0"
          />
        </label>
        <button type="submit">Search</button>
      </form>
      {loading ? <LoadingState label="Φόρτωση αποτελεσμάτων…" /> : null}
      {!loading && error ? <ErrorState error={error} /> : null}
      {!loading && !error && result && result.rows.length === 0 ? (
        <EmptyState title="Καμία μετοχή δεν ταιριάζει στα φίλτρα." />
      ) : null}
      {!loading && !error && result && result.rows.length > 0 ? (
        <>
          <p className="muted pager-label">
            {from}–{to} από {total}
          </p>
          <ScanTable rows={result.rows} onOpen={onOpen} />
          <div className="pager">
            <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              Previous
            </button>
            <button
              type="button"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        </>
      ) : null}
    </section>
  );
}

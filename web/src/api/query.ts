export interface ScreenerQuery {
  q: string;
  status: "" | "PASS" | "BLOCKED";
  minScore: string;
  limit: number;
  offset: number;
}

/** Search URL for the latest operational scan. It does not request history. */
export function symbolsPath(query: ScreenerQuery): string {
  const params = new URLSearchParams();
  const text = query.q.trim();
  if (text) params.set("q", text);
  if (query.status) params.set("status", query.status);
  const score = query.minScore.trim();
  if (score) params.set("min_score", score);
  params.set("limit", String(query.limit));
  params.set("offset", String(query.offset));
  return `/api/v1/symbols?${params.toString()}`;
}

export function symbolPath(symbol: string, range: "24H" | "3D" | "5D"): string {
  return `/api/v1/symbols/${encodeURIComponent(symbol)}?range=${range}`;
}

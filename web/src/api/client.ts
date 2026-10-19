import { parseLatestScan, parseRules, parseSymbolSearch, parseSymbolView, ResponseShapeError } from "./parse";
import { symbolPath, symbolsPath, type ScreenerQuery } from "./query";
import type { ApiFailure, HistoryRange, LatestScan, RulesCatalog, SymbolSearch, SymbolView } from "./types";

const base = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(failure: ApiFailure) {
    super(failure.code ?? `http_${failure.status}`);
    this.name = "ApiError";
    this.status = failure.status;
    this.code = failure.code;
  }
}

export function fetchRules(): Promise<RulesCatalog> {
  return request("/api/v1/rules", parseRules);
}

export function fetchLatestScan(): Promise<LatestScan> {
  return request("/api/v1/scan/latest", parseLatestScan);
}

export function fetchSymbols(query: ScreenerQuery): Promise<SymbolSearch> {
  return request(symbolsPath(query), parseSymbolSearch);
}

export function fetchSymbol(symbol: string, range: HistoryRange): Promise<SymbolView> {
  return request(symbolPath(symbol, range), parseSymbolView);
}

async function request<T>(path: string, parse: (value: unknown) => T): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError({ status: 0, code: "network" });
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const code = errorCode(body);
    throw new ApiError({ status: response.status, code });
  }
  try {
    return parse(body);
  } catch (error) {
    if (error instanceof ResponseShapeError) {
      throw new ApiError({ status: response.status, code: "bad_response" });
    }
    throw error;
  }
}

function errorCode(body: unknown): string | null {
  if (typeof body !== "object" || body === null || !("error" in body)) return null;
  const code = (body as { error: unknown }).error;
  return typeof code === "string" ? code : null;
}

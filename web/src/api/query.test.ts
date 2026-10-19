import { describe, expect, it } from "vitest";
import { symbolPath, symbolsPath } from "./query";

describe("screener query", () => {
  it("asks the search endpoint for one page and does not request history", () => {
    const path = symbolsPath({ q: " aa ", status: "BLOCKED", minScore: "5", limit: 25, offset: 50 });
    expect(path.startsWith("/api/v1/symbols?")).toBe(true);
    expect(path).not.toContain("/symbols/");
    expect(path).not.toContain("range=");
    const params = new URLSearchParams(path.split("?")[1]);
    expect(params.get("q")).toBe("aa");
    expect(params.get("status")).toBe("BLOCKED");
    expect(params.get("min_score")).toBe("5");
    expect(params.get("limit")).toBe("25");
    expect(params.get("offset")).toBe("50");
  });

  it("requests one symbol range from the symbol endpoint", () => {
    expect(symbolPath("AAPL", "5D")).toBe("/api/v1/symbols/AAPL?range=5D");
  });
});

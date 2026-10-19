export interface ChartPoint {
  label: string;
  value: number | null;
  kind: "CURRENT" | "LOOKBACK" | null;
}

export interface PlottedPoint {
  index: number;
  x: number;
  y: number;
  value: number;
  kind: ChartPoint["kind"];
}

/** Contiguous non-null points. A missing value breaks the line and is not drawn as zero. */
export function chartSegments(points: ChartPoint[], width: number, height: number): PlottedPoint[][] {
  const values = points.map((point) => point.value).filter((value): value is number => value !== null);
  if (values.length === 0 || points.length === 0) return [];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 8;
  const innerWidth = Math.max(width - pad * 2, 1);
  const innerHeight = Math.max(height - pad * 2, 1);
  const step = points.length === 1 ? 0 : innerWidth / (points.length - 1);
  const segments: PlottedPoint[][] = [];
  let current: PlottedPoint[] = [];
  points.forEach((point, index) => {
    if (point.value === null) {
      if (current.length) segments.push(current);
      current = [];
      return;
    }
    const y = pad + ((max - point.value) / span) * innerHeight;
    current.push({
      index,
      x: pad + index * step,
      y,
      value: point.value,
      kind: point.kind,
    });
  });
  if (current.length) segments.push(current);
  return segments;
}

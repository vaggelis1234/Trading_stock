import { chartSegments, type ChartPoint } from "../charts";

const WIDTH = 640;
const HEIGHT = 180;

export function MetricChart({ title, points }: { title: string; points: ChartPoint[] }) {
  const segments = chartSegments(points, WIDTH, HEIGHT);
  const plotted = segments.flat();
  return (
    <section className="chart-card">
      <header>
        <h3>{title}</h3>
        <span>{plotted.length} values</span>
      </header>
      {plotted.length === 0 ? (
        <p className="muted">No values in this window.</p>
      ) : (
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={title}>
          {segments.map((segment, index) => (
            <polyline
              key={index}
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              points={segment.map((point) => `${point.x},${point.y}`).join(" ")}
            />
          ))}
          {plotted.map((point) => (
            <circle
              key={point.index}
              cx={point.x}
              cy={point.y}
              r={point.kind === "CURRENT" ? 4.5 : 2.5}
              className={point.kind === "LOOKBACK" ? "dot lookback" : "dot decision"}
            >
              <title>
                {points[point.index]?.label} {point.kind ?? "legacy"} {point.value}
              </title>
            </circle>
          ))}
        </svg>
      )}
    </section>
  );
}

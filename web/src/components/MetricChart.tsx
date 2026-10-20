import { useEffect, useMemo, useRef, useState } from "react";
import { plotSessionChart, sessionTimeSlots, type ChartPoint, type TimeSlot } from "../charts";
import { formatNumber } from "../format";

const WIDTH = 720;
const HEIGHT = 280;
const MIN_BARS = 3;

export function MetricChart({ title, points, digits }: { title: string; points: ChartPoint[]; digits: number }) {
  const slots = useMemo(() => sessionTimeSlots(points), [points]);
  const slotKey = slots.map((slot) => `${slot.dateLabel}|${slot.axisLabel}|${slot.value ?? ""}`).join(";");
  const [stored, setStored] = useState({ key: "", start: 0, count: 0 });
  const view = stored.key === slotKey ? stored : { key: slotKey, start: 0, count: slots.length };
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const drag = useRef<{ x: number; start: number } | null>(null);
  const viewRef = useRef(view);
  const slotKeyRef = useRef(slotKey);
  viewRef.current = view;
  slotKeyRef.current = slotKey;

  function setView(next: { start: number; count: number }) {
    setStored({ key: slotKey, ...next });
    setHover(null);
  }

  useEffect(() => {
    const node = svgRef.current;
    if (!node) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = node.getBoundingClientRect();
      const ratio = rect.width === 0 ? 0.5 : (event.clientX - rect.left) / rect.width;
      const current = viewRef.current;
      const nextCount = event.deltaY > 0 ? current.count * 1.4 : current.count / 1.4;
      const next = zoomWindow(slots.length, current, ratio, nextCount);
      setStored({ key: slotKeyRef.current, ...next });
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [slots.length]);

  const visible = slots.slice(view.start, view.start + view.count);
  const layout = plotSessionChart(visible, WIDTH, HEIGHT, digits);
  const zoomed = view.start !== 0 || view.count !== slots.length;
  const hoverSlot = hover === null ? null : visible[hover] ?? null;

  return (
    <section className="chart-card">
      <header>
        <div>
          <h3>{title}</h3>
          <p className="muted">{caption(layout, hoverSlot, digits, slots.length, visible.length)}</p>
        </div>
        <div className="chart-tools">
          <button type="button" onClick={() => setView(zoomWindow(slots.length, view, 0.5, view.count / 1.5))} disabled={view.count <= Math.min(MIN_BARS, slots.length)}>
            +
          </button>
          <button type="button" onClick={() => setView(zoomWindow(slots.length, view, 0.5, view.count * 1.5))} disabled={!zoomed}>
            −
          </button>
          <button type="button" onClick={() => setView({ start: 0, count: slots.length })} disabled={!zoomed}>
            Reset
          </button>
        </div>
      </header>
      {layout === null ? (
        <p className="muted">No session bars in this window.</p>
      ) : (
        <svg
          ref={svgRef}
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`${title} from ${layout.yMin} to ${layout.yMax}`}
          className="metric-chart"
          onDoubleClick={() => setView({ start: 0, count: slots.length })}
          onPointerDown={(event) => {
            if (event.button !== 0) return;
            drag.current = { x: event.clientX, start: view.start };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            if (!drag.current) return;
            const rect = event.currentTarget.getBoundingClientRect();
            const plotWidth = rect.width * ((layout.plot.right - layout.plot.left) / WIDTH);
            const shifted = Math.round((-(event.clientX - drag.current.x) / Math.max(plotWidth, 1)) * view.count);
            const count = view.count;
            const start = clamp(drag.current.start + shifted, 0, Math.max(slots.length - count, 0));
            setView({ start, count });
          }}
          onPointerUp={() => {
            drag.current = null;
          }}
          onPointerLeave={() => setHover(null)}
        >
          {layout.yTicks.map((tick) => (
            <line key={`grid-${tick.label}`} className="chart-gridline" x1={layout.plot.left} x2={layout.plot.right} y1={tick.at} y2={tick.at} />
          ))}
          <line className="chart-frame" x1={layout.plot.left} x2={layout.plot.left} y1={layout.plot.top} y2={layout.plot.bottom} />
          <line className="chart-frame" x1={layout.plot.left} x2={layout.plot.right} y1={layout.plot.bottom} y2={layout.plot.bottom} />
          {layout.slotSpans.map((span) => {
            const slot = visible[span.index];
            if (!slot || slot.weight >= 1) return null;
            return (
              <g key={`half-${span.index}`}>
                <rect className="chart-half" x={span.x} y={layout.plot.top} width={span.width} height={layout.plot.bottom - layout.plot.top} />
                <text className="chart-half-label" x={span.x + span.width / 2} y={layout.plot.top + 14} textAnchor="middle">
                  {span.width >= 78 ? "15:30–16:00" : "30m"}
                </text>
              </g>
            );
          })}
          {layout.segments.map((segment, index) => (
            <polyline key={index} className="chart-line" points={segment.map((point) => `${point.x},${point.y}`).join(" ")} />
          ))}
          {layout.yTicks.map((tick) => (
            <text key={`y-${tick.label}`} className="chart-axis" x={layout.plot.left - 8} y={tick.at + 4} textAnchor="end">
              {tick.label}
            </text>
          ))}
          {layout.xTicks.map((tick) => (
            <text
              key={`x-${tick.row}-${tick.at}-${tick.label}`}
              className="chart-axis"
              x={tick.at}
              y={tick.row === "date" ? layout.plot.bottom + 34 : layout.plot.bottom + 16}
              textAnchor="middle"
            >
              {tick.label}
            </text>
          ))}
          {layout.segments.flat().map((point) => (
            <circle
              key={point.index}
              cx={point.x}
              cy={point.y}
              r={point.kind === "CURRENT" ? 4.5 : 3}
              className={point.kind === "LOOKBACK" ? "dot lookback" : "dot decision"}
            />
          ))}
          {hover !== null && layout.slotSpans[hover] ? (
            <line
              className="chart-hover"
              x1={layout.slotSpans[hover].x + layout.slotSpans[hover].width / 2}
              x2={layout.slotSpans[hover].x + layout.slotSpans[hover].width / 2}
              y1={layout.plot.top}
              y2={layout.plot.bottom}
            />
          ) : null}
          {layout.slotSpans.map((span) => (
            <rect
              key={`hit-${span.index}`}
              className="chart-hit"
              x={span.x}
              y={layout.plot.top}
              width={span.width}
              height={layout.plot.bottom - layout.plot.top}
              onPointerEnter={() => setHover(span.index)}
            />
          ))}
        </svg>
      )}
    </section>
  );
}

function caption(
  layout: { yMin: number; yMax: number } | null,
  hover: TimeSlot | null,
  digits: number,
  total: number,
  shown: number,
): string {
  if (hover && hover.value !== null) {
    return `${hover.dateLabel} ${hover.clock} · ${formatNumber(hover.value, digits)}`;
  }
  if (!layout) return "New York session hours";
  const range = `${formatNumber(layout.yMin, digits)}–${formatNumber(layout.yMax, digits)}`;
  const bars = shown === total ? `${total} bars` : `${shown} of ${total} bars`;
  return `New York · ${range} · ${bars}`;
}

function zoomWindow(total: number, current: { start: number; count: number }, ratio: number, nextCount: number): { start: number; count: number } {
  if (total === 0) return { start: 0, count: 0 };
  const count = clamp(Math.round(nextCount), Math.min(MIN_BARS, total), total);
  const anchor = current.start + clamp(ratio, 0, 1) * current.count;
  const start = clamp(Math.round(anchor - ratio * count), 0, total - count);
  return { start, count };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

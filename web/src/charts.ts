export interface ChartPoint {
  label: string;
  value: number | null;
  kind: "CURRENT" | "LOOKBACK" | null;
  /** ISO timestamp of the stored snapshot. Session charts ignore points without one. */
  at?: string;
  /** Universe charts use completed 30-minute scans. */
  interval?: "30m";
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

const NEW_YORK = "America/New_York";
const SCAN_GRACE_MINUTES = 5;

interface SessionWindow {
  start: number;
  end: number;
  weight: number;
  label: string;
  key: string;
}

/** Cash session. Thirteen completed 30-minute bars from 09:30 through 15:30. */
const HALF_HOUR_WINDOWS: SessionWindow[] = Array.from({ length: 13 }, (_, index) => {
  const start = 9 * 60 + 30 + index * 30;
  const end = start + 30;
  const hour = Math.floor(start / 60);
  const minute = start % 60;
  const label = `${hour}:${String(minute).padStart(2, "0")}`;
  return { start, end, weight: 1, label, key: label };
});

export interface TimeSlot {
  at: string | null;
  /** Discrete axis label: a clock hour, or the closing half-hour print. */
  axisLabel: string;
  dateLabel: string;
  showDate: boolean;
  /** Observation clock in New York. An hourly bucket keeps the latest print. */
  clock: string;
  value: number | null;
  kind: ChartPoint["kind"];
  /** 1 for an hourly bar, 0.5 for the 15:30–16:00 bar. */
  weight: number;
}

interface NyParts {
  dateKey: string;
  dateLabel: string;
  minutes: number;
  hour: number;
  minute: number;
}

interface Bucket {
  dateKey: string;
  dateLabel: string;
  key: string;
  at: string;
  clock: string;
  value: number | null;
  kind: ChartPoint["kind"];
  instant: number;
  distance: number;
}

/**
 * One slot per cash-session window, from 09:30 through 15:30–16:00.
 *
 * Closed hours and weekends are omitted. A missing window between the first
 * and last print of a day stays empty so the axis does not skip it. Several
 * prints in one window collapse to the one nearest the window start.
 */
export function sessionTimeSlots(points: ChartPoint[], interval: "30m" = "30m"): TimeSlot[] {
  const windows = HALF_HOUR_WINDOWS;
  const selected = points.filter((point) => (point.interval ?? interval) === "30m");
  const buckets = new Map<string, Bucket>();
  for (const point of selected) {
    if (!point.at) continue;
    const ny = nyParts(point.at);
    if (!ny) continue;
    const placed = classifyWindow(ny.minutes, windows);
    if (!placed) continue;
    const key = `${ny.dateKey}|${placed.window.key}`;
    const instant = Date.parse(point.at);
    const distance = placed.distance;
    const existing = buckets.get(key);
    if (existing && (existing.distance < distance || (existing.distance === distance && existing.instant > instant))) continue;
    buckets.set(key, {
      dateKey: ny.dateKey,
      dateLabel: ny.dateLabel,
      key: placed.window.key,
      at: point.at,
      clock: `${pad(ny.hour)}:${pad(ny.minute)}`,
      value: point.value,
      kind: point.kind,
      instant,
      distance,
    });
  }

  const byDate = new Map<string, Bucket[]>();
  for (const bucket of buckets.values()) {
    const list = byDate.get(bucket.dateKey) ?? [];
    list.push(bucket);
    byDate.set(bucket.dateKey, list);
  }

  const slots: TimeSlot[] = [];
  for (const dateKey of [...byDate.keys()].sort()) {
    const day = byDate.get(dateKey) ?? [];
    const byKey = new Map(day.map((bucket) => [bucket.key, bucket]));
    const indexes = day.map((bucket) => windows.findIndex((window) => window.key === bucket.key));
    const start = Math.min(...indexes);
    const end = Math.max(...indexes);
    for (let index = start; index <= end; index += 1) {
      const window = windows[index];
      const bucket = byKey.get(window.key);
      const slot = bucket ? toSlot(bucket, window.label, window.weight) : emptySlot(day[0], window.label, window.weight);
      slots.push({ ...slot, showDate: index === start });
    }
  }
  return slots;
}

export function sessionWindowLabel(iso: string, interval: "30m" = "30m"): string {
  const ny = nyParts(iso);
  if (!ny) return iso;
  const placed = classifyWindow(ny.minutes, HALF_HOUR_WINDOWS);
  void interval;
  const window = placed?.window;
  const range = window ? `${window.label}–${pad(Math.floor(window.end / 60))}:${pad(window.end % 60)}` : "";
  const clock = window ? range : `${pad(ny.hour)}:${pad(ny.minute)}`;
  return `${ny.dateLabel} ${clock} ET`;
}

export function metricDomain(values: number[]): { min: number; max: number } | null {
  if (values.length === 0) return null;
  return { min: Math.min(...values), max: Math.max(...values) };
}

export interface ChartTick {
  at: number;
  label: string;
  row: "hour" | "date";
}

export interface SlotSpan {
  index: number;
  x: number;
  width: number;
}

export interface ChartLayout {
  segments: PlottedPoint[][];
  xTicks: ChartTick[];
  yTicks: ChartTick[];
  yMin: number;
  yMax: number;
  plot: { left: number; top: number; right: number; bottom: number };
  slotSpans: SlotSpan[];
}

const PLOT_LEFT = 58;
const PLOT_RIGHT = 16;
const PLOT_TOP = 14;
const PLOT_BOTTOM = 56;

/** Map session slots into the plot. Y runs from the data minimum to the data maximum. */
export function plotSessionChart(slots: TimeSlot[], width: number, height: number, digits: number): ChartLayout | null {
  const values = slots.map((slot) => slot.value).filter((value): value is number => value !== null);
  const domain = metricDomain(values);
  if (!domain || slots.length === 0) return null;
  const plot = {
    left: PLOT_LEFT,
    top: PLOT_TOP,
    right: Math.max(width - PLOT_RIGHT, PLOT_LEFT + 1),
    bottom: Math.max(height - PLOT_BOTTOM, PLOT_TOP + 1),
  };
  const total = slots.reduce((sum, slot) => sum + slot.weight, 0) || 1;
  const plotWidth = plot.right - plot.left;
  let cursor = 0;
  const slotSpans: SlotSpan[] = slots.map((slot, index) => {
    const band = (slot.weight / total) * plotWidth;
    const span = { index, x: plot.left + cursor, width: band };
    cursor += band;
    return span;
  });
  const yOf = (value: number) => {
    if (domain.max === domain.min) return (plot.top + plot.bottom) / 2;
    return plot.bottom - ((value - domain.min) / (domain.max - domain.min)) * (plot.bottom - plot.top);
  };
  const segments: PlottedPoint[][] = [];
  let current: PlottedPoint[] = [];
  let currentDate = "";
  slots.forEach((slot, index) => {
    const dayBreak = current.length > 0 && slot.dateLabel !== currentDate;
    if (slot.value === null || dayBreak) {
      if (current.length) segments.push(current);
      current = [];
      if (slot.value === null) return;
    }
    currentDate = slot.dateLabel;
    const span = slotSpans[index];
    current.push({
      index,
      x: span.x + span.width / 2,
      y: yOf(slot.value),
      value: slot.value,
      kind: slot.kind,
    });
  });
  if (current.length) segments.push(current);

  const yTicks: ChartTick[] = [];
  if (domain.max === domain.min) {
    yTicks.push({ at: yOf(domain.min), label: formatTick(domain.min, digits), row: "hour" });
  } else {
    for (const value of [domain.max, (domain.max + domain.min) / 2, domain.min]) {
      yTicks.push({ at: yOf(value), label: formatTick(value, digits), row: "hour" });
    }
  }

  const xTicks: ChartTick[] = [];
  const labeled: number[] = [];
  const centerOf = (index: number) => slotSpans[index].x + slotSpans[index].width / 2;
  const keep = (index: number) => slots[index].showDate;
  slotSpans.forEach((span, index) => {
    const at = span.x + span.width / 2;
    if (keep(index)) {
      while (labeled.length > 0 && !keep(labeled[labeled.length - 1]) && at - centerOf(labeled[labeled.length - 1]) < 56) {
        labeled.pop();
      }
      labeled.push(index);
      return;
    }
    const previous = labeled.length > 0 ? centerOf(labeled[labeled.length - 1]) : -Infinity;
    if (at - previous < 52) return;
    labeled.push(index);
  });
  for (const index of labeled) {
    xTicks.push({ at: centerOf(index), label: slots[index].axisLabel, row: "hour" });
  }
  let dayStart = 0;
  slots.forEach((slot, index) => {
    const next = slots[index + 1];
    if (next && next.dateLabel === slot.dateLabel) return;
    const from = slotSpans[dayStart].x;
    const to = slotSpans[index].x + slotSpans[index].width;
    xTicks.push({ at: (from + to) / 2, label: slot.dateLabel, row: "date" });
    dayStart = index + 1;
  });

  return { segments, xTicks, yTicks, yMin: domain.min, yMax: domain.max, plot, slotSpans };
}

function toSlot(bucket: Bucket, axisLabel: string, weight: number): TimeSlot {
  return {
    at: bucket.at,
    axisLabel,
    dateLabel: bucket.dateLabel,
    showDate: false,
    clock: bucket.clock,
    value: bucket.value,
    kind: bucket.kind,
    weight,
  };
}

function emptySlot(sample: Bucket, axisLabel: string, weight: number): TimeSlot {
  return {
    at: null,
    axisLabel,
    dateLabel: sample.dateLabel,
    showDate: false,
    clock: axisLabel,
    value: null,
    kind: null,
    weight,
  };
}

function classifyWindow(minutes: number, windows: SessionWindow[] = HALF_HOUR_WINDOWS): { window: SessionWindow; distance: number } | null {
  for (const window of windows) {
    const scan = window.end + 15;
    if (minutes >= scan && minutes < scan + SCAN_GRACE_MINUTES) return { window, distance: 0 };
  }
  for (const window of windows) {
    const closes = window.end === 16 * 60;
    if (minutes < window.start) continue;
    if (closes ? minutes > window.end : minutes >= window.end) continue;
    const distance = Math.abs(minutes - window.start);
    // A clock-hour print such as 10:00 is not the 09:30 window. The 16:00
    // print is the right edge of the closing half hour and still belongs there.
    if (distance > 15 && minutes !== window.end) return null;
    return { window, distance: minutes === window.end ? 0 : distance };
  }
  return null;
}

function nyParts(iso: string): NyParts | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: NEW_YORK,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const pick = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? "";
  let hourText = pick("hour");
  if (hourText === "24") hourText = "00";
  const hour = Number(hourText);
  const minute = Number(pick("minute"));
  if (!Number.isInteger(hour) || !Number.isInteger(minute)) return null;
  const day = pick("day");
  const month = pick("month");
  return {
    dateKey: `${pick("year")}-${month}-${day}`,
    dateLabel: `${day}/${month}`,
    minutes: hour * 60 + minute,
    hour,
    minute,
  };
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function formatTick(value: number, digits: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

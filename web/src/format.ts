import type { MovementLabel, SnapshotKind } from "./api/types";

const ATHENS = "Europe/Athens";
const NEW_YORK = "America/New_York";

export function formatAthens(value: string | null): string {
  if (!value) return "—";
  return formatZoned(value, ATHENS, "en-GB");
}

export function formatNewYork(value: string | null): string {
  if (!value) return "—";
  return formatZoned(value, NEW_YORK, "en-US");
}

const SIP_DELAY_MINUTES = 15;
/** Session windows, in minutes from midnight New York. The last one is the closing half hour. */
const SESSION_WINDOWS: ReadonlyArray<readonly [number, number]> = [
  [9 * 60 + 30, 10 * 60 + 30],
  [10 * 60 + 30, 11 * 60 + 30],
  [11 * 60 + 30, 12 * 60 + 30],
  [12 * 60 + 30, 13 * 60 + 30],
  [13 * 60 + 30, 14 * 60 + 30],
  [14 * 60 + 30, 15 * 60 + 30],
  [15 * 60 + 30, 16 * 60],
];

/**
 * Latest session window whose SIP print is available at the scan.
 * A window ending at 13:30 can be read from 13:45, so a 13:30 scan still shows 11:30-12:30.
 */
export function formatNewYorkPeriod(value: string | null): string {
  const parts = zonedParts(value, NEW_YORK);
  if (!parts) return "—";
  const clock = Number(parts.hour) * 60 + Number(parts.minute);
  if (!Number.isInteger(clock)) return "—";
  let ready: readonly [number, number] | null = null;
  for (const sessionWindow of SESSION_WINDOWS) {
    if (clock >= sessionWindow[1] + SIP_DELAY_MINUTES) ready = sessionWindow;
  }
  if (!ready) return "—";
  return `${clockLabel(ready[0])}-${clockLabel(ready[1])}`;
}

function formatZoned(value: string, timeZone: string, locale: string): string {
  const parts = zonedParts(value, timeZone, locale);
  if (!parts) return "—";
  return `${parts.day}/${parts.month}/${parts.year}, ${parts.hour}:${parts.minute} ${parts.zone}`;
}

function zonedParts(value: string | null, timeZone: string, locale = "en-US"): { day: string; month: string; year: string; hour: string; minute: string; zone: string } | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat(locale, {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZoneName: "short",
  }).formatToParts(date);
  const pick = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? "";
  let hour = pick("hour");
  if (hour === "24") hour = "00";
  return {
    day: pick("day"),
    month: pick("month"),
    year: pick("year"),
    hour,
    minute: pick("minute"),
    zone: pick("timeZoneName"),
  };
}

function clockLabel(minutes: number): string {
  const hour = Math.floor(minutes / 60);
  const minute = minutes % 60;
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

export function formatNumber(value: number | null, digits: number): string {
  if (value === null || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatAdv(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

export function movementText(label: MovementLabel, movement: number | null): string {
  if (label === "new") return "NEW";
  if (label === "up" && movement !== null) return `↑${movement}`;
  if (label === "down" && movement !== null) return `↓${Math.abs(movement)}`;
  if (label === "flat") return "—";
  return "—";
}

/** History rows are stored hours. Only CURRENT, or a legacy null kind on the page header, is the decision. */
export function snapshotRole(kind: SnapshotKind): "decision" | "lookback" | "legacy" {
  if (kind === "CURRENT") return "decision";
  if (kind === "LOOKBACK") return "lookback";
  return "legacy";
}

export function snapshotLabel(kind: SnapshotKind): string {
  const role = snapshotRole(kind);
  if (role === "decision") return "CURRENT";
  if (role === "lookback") return "LOOKBACK";
  return "legacy";
}

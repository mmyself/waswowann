import { DateTime } from "luxon";
import type { ObservationSummary, TimeExtent } from "./types";

export type FilterMode = "all" | "window";
export interface TimeWindow { start: number; end: number }
export const DISPLAY_TIMEZONE = process.env.NEXT_PUBLIC_DISPLAY_TIMEZONE ?? "Europe/Berlin";

export function toEpoch(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = DateTime.fromISO(value, { setZone: true });
  return parsed.isValid ? parsed.toUTC().toMillis() : null;
}

export function toLocalInput(epoch: number, zone = DISPLAY_TIMEZONE, exact = false): string {
  const local = DateTime.fromMillis(epoch, { zone });
  return exact ? local.toISO({ includeOffset: false, suppressMilliseconds: true, suppressSeconds: true }) ?? "" : local.toFormat("yyyy-LL-dd'T'HH:mm");
}

export function fromLocalInput(value: string, zone = DISPLAY_TIMEZONE): number | null {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(value);
  if (!match) return null;
  const local = `${match[1]}:${match[2] ?? "00"}.${(match[3] ?? "").padEnd(3, "0")}`;
  const parsed = DateTime.fromISO(local, { zone });
  // Reject DST gaps and ambiguous wall-clock times rather than silently shifting them.
  if (!parsed.isValid || parsed.getPossibleOffsets().length !== 1 || parsed.toISO({ includeOffset: false }) !== local) return null;
  return parsed.toMillis();
}

export function extentToWindow(extent: TimeExtent | null): TimeWindow | null {
  const start = toEpoch(extent?.min);
  const end = toEpoch(extent?.max);
  return start === null || end === null ? null : { start, end };
}

export function filterObservations(
  observations: ObservationSummary[], mode: FilterMode, window: TimeWindow | null,
): ObservationSummary[] {
  if (mode === "all" || !window) return observations;
  return observations.filter((item) => {
    const start = toEpoch(item.first_observed_at);
    const end = toEpoch(item.last_observed_at);
    return start !== null && end !== null && start <= window.end && end >= window.start;
  });
}

export function clampWindow(window: TimeWindow, extent: TimeWindow): TimeWindow {
  const start = Math.min(extent.end, Math.max(extent.start, window.start));
  const end = Math.min(extent.end, Math.max(start, window.end));
  return { start, end };
}

export function mergeFutureExtent(base: TimeWindow | null, futureTimesMs: number[]): TimeWindow | null {
  const validFuture = futureTimesMs.filter((ms) => Number.isFinite(ms));
  if (!validFuture.length) return base;
  const maxFuture = Math.max(...validFuture);
  const minFuture = Math.min(...validFuture);
  if (!base) return { start: minFuture, end: maxFuture };
  return { start: Math.min(base.start, minFuture), end: Math.max(base.end, maxFuture) };
}

export function formatPhotoDate(epoch: number | null, format = "d. LLL"): string {
  return epoch === null ? "Ohne Datum" : DateTime.fromMillis(epoch, { zone: DISPLAY_TIMEZONE, locale: "de" }).toFormat(format);
}

export function formatDateSpan(first: string | null, last: string | null): string {
  const startMs = toEpoch(first);
  const endMs = toEpoch(last);
  if (startMs === null || endMs === null) return "Ohne Datum";
  const start = DateTime.fromMillis(startMs, { zone: DISPLAY_TIMEZONE, locale: "de" });
  const end = DateTime.fromMillis(endMs, { zone: DISPLAY_TIMEZONE, locale: "de" });
  if (start.hasSame(end, "day")) return start.toFormat("d. LLL yyyy");
  if (start.hasSame(end, "month")) return `${start.toFormat("d.")}–${end.toFormat("d. LLL yyyy")}`;
  if (start.hasSame(end, "year")) return `${start.toFormat("d. LLL")} – ${end.toFormat("d. LLL yyyy")}`;
  return `${start.toFormat("d. LLL yyyy")} – ${end.toFormat("d. LLL yyyy")}`;
}

export function formatDate(value: string | null | undefined, zone = DISPLAY_TIMEZONE): string {
  if (!value) return "Unbekannt";
  const parsed = DateTime.fromISO(value, { setZone: true }).setZone(zone);
  return parsed.isValid ? parsed.toFormat("d. LLL yyyy, HH:mm") : "Unbekannt";
}

export function formatRange(window: TimeWindow | null, zone = DISPLAY_TIMEZONE): string {
  if (!window) return "Kein datierter Zeitraum";
  const start = DateTime.fromMillis(window.start, { zone: "utc" }).setZone(zone).toFormat("d. LLL yyyy, HH:mm");
  const end = DateTime.fromMillis(window.end, { zone: "utc" }).setZone(zone).toFormat("d. LLL yyyy, HH:mm");
  return `${start} – ${end} (${zone})`;
}

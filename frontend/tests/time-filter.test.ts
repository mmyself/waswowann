import { describe, expect, it } from "vitest";
import { filterObservations, fromLocalInput, toEpoch } from "../src/lib/time-filter";
import type { ObservationSummary } from "../src/lib/types";

const base = { title: "Fund", category: "Ding", lat: null, lon: null, thumbnail_url: null, source: null, media_count: 1, location_measurement_count: 0, sigma_meters: null, sigma_seconds: null } as const;
const observations: ObservationSummary[] = [
  { ...base, id: "early", observed_at: "2026-05-01T10:00:00Z", first_observed_at: "2026-05-01T10:00:00Z", last_observed_at: "2026-05-01T10:00:00Z" },
  { ...base, id: "late", observed_at: "2026-05-01T11:00:00Z", first_observed_at: "2026-05-01T11:00:00Z", last_observed_at: "2026-05-01T11:00:00Z" },
  { ...base, id: "unknown", observed_at: null, first_observed_at: null, last_observed_at: null },
];

describe("observation time filtering", () => {
  it("includes both window boundaries and excludes unknown dates", () => {
    expect(filterObservations(observations, "window", {
      start: Date.parse("2026-05-01T10:00:00Z"),
      end: Date.parse("2026-05-01T11:00:00Z"),
    }).map(({ id }) => id)).toEqual(["early", "late"]);
  });

  it("keeps an object whose photo range spans the window when its representative date is outside", () => {
    const object = { ...observations[1], first_observed_at: "2026-05-01T10:00:00Z" };
    expect(filterObservations([object], "window", {
      start: Date.parse("2026-05-01T10:15:00Z"), end: Date.parse("2026-05-01T10:45:00Z"),
    })).toEqual([object]);
  });

  it("keeps dated and undated entries in all mode", () => {
    expect(filterObservations(observations, "all", null)).toHaveLength(3);
  });

  it("parses offset timestamps and rejects DST-ambiguous local inputs", () => {
    expect(toEpoch("2026-05-01T12:00:00+02:00")).toBe(Date.parse("2026-05-01T10:00:00Z"));
    expect(fromLocalInput("2026-10-25T02:30", "Europe/Berlin")).toBeNull();
    expect(fromLocalInput("2026-03-29T02:30", "Europe/Berlin")).toBeNull();
  });
});

import { describe, expect, it } from "vitest";
import { photoLocationFeatures, uncertaintyFeatures } from "../src/lib/contours";
import type { MediaItem, ObservationSummary } from "../src/lib/types";

const timestamp = "2026-11-16T10:00:00Z";
const observation: ObservationSummary = {
  id: "obs-1",
  title: "Fund",
  category: "Pilz",
  lat: 53.5501,
  lon: 9.9901,
  observed_at: timestamp,
  first_observed_at: timestamp,
  last_observed_at: timestamp,
  thumbnail_url: null,
  source: "local",
  sigma_meters: 6,
  sigma_seconds: 1,
  media_count: 8,
  location_measurement_count: 8,
};

const photo: MediaItem = {
  id: "photo-1", filename: "first.jpg", source: "local", width: 100, height: 100,
  image_url: "/photo", thumbnail_url: "/thumb", exif: {}, observed_at_ms: Date.parse(timestamp),
  time_source: "exif", lat: 53.55, lon: 9.99, sigma_meters: 10, sigma_seconds: 1, location_source: "exif",
};

describe("Kalman contours and optional photo GPS dots", () => {
  it("generates one 68%/95% polygon pair per located object irrespective of its photo count", () => {
    const { features } = uncertaintyFeatures([observation, { ...observation, id: "obs-2", lat: null }]);
    expect(features.map((f) => f.id)).toEqual(["obs-1-68", "obs-1-95"]);
    const ring68 = features[0].geometry.coordinates[0];
    const ring95 = features[1].geometry.coordinates[0];
    expect(ring68.at(-1)).toEqual(ring68[0]);
    const maxLat68 = Math.max(...ring68.map((p) => p[1])) - 53.5501;
    const maxLat95 = Math.max(...ring95.map((p) => p[1])) - 53.5501;
    expect(maxLat95 / maxLat68).toBeCloseTo(2.45 / 1.51, 4);
  });

  it("represents only geolocated photos as raw points", () => {
    const { features } = photoLocationFeatures([photo, { ...photo, id: "photo-2", lat: 0, lon: 0 }, { ...photo, id: "photo-3", lat: null }]);
    expect(features.map((feature) => feature.geometry.coordinates)).toEqual([[9.99, 53.55], [0, 0]]);
  });
});

import type { FeatureCollection, Point, Polygon } from "geojson";
import type { GoalStatus, MediaItem, ObservationSummary, SpatialPrediction } from "@/lib/types";

const CONTOUR_LEVELS = [
  [68, 1.51],
  [95, 2.45],
] as const;

export function isMappable(lat: number, lon: number): boolean {
  return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 85.051129 && Math.abs(lon) <= 180;
}

export function uncertaintyFeatures(observations: ObservationSummary[]): FeatureCollection<Polygon, { level: number; id: string }> {
  return {
    type: "FeatureCollection",
    features: observations.flatMap(({ id, lat, lon, sigma_meters }) => {
      if (lat === null || lon === null || sigma_meters === null || !isMappable(lat, lon)) return [];
      return CONTOUR_LEVELS.flatMap(([level, multiplier]) => {
        const ring = ellipseRing(lon, lat, sigma_meters * multiplier);
        if (!ring) return [];
        return [{ type: "Feature", id: `${id}-${level}`, geometry: { type: "Polygon", coordinates: [ring] }, properties: { id, level } }];
      });
    }),
  };
}

export function predictedUncertaintyFeatures(
  predictions: SpatialPrediction[]
): FeatureCollection<Polygon, { level: number; id: string; status: GoalStatus | "neutral"; label: string }> {
  return {
    type: "FeatureCollection",
    features: predictions.flatMap(({ id, lat, lon, sigma_meters, goal_status, label }) => {
      if (!isMappable(lat, lon) || !Number.isFinite(sigma_meters) || sigma_meters <= 0) return [];
      const status = goal_status ?? "neutral";
      return CONTOUR_LEVELS.flatMap(([level, multiplier]) => {
        const ring = ellipseRing(lon, lat, sigma_meters * multiplier);
        if (!ring) return [];
        return [{ type: "Feature", id: `pred-${id}-${level}`, geometry: { type: "Polygon", coordinates: [ring] }, properties: { id, level, status, label } }];
      });
    }),
  };
}

export function predictedPointFeatures(
  predictions: SpatialPrediction[]
): FeatureCollection<
  Point,
  { id: string; entity_id: string; legacy_obs_id: string; label: string; status: GoalStatus | "neutral"; sigma_meters: number; goal_probability: number | null }
> {
  return {
    type: "FeatureCollection",
    features: predictions.flatMap((pred) => {
      if (!isMappable(pred.lat, pred.lon)) return [];
      return [
        {
          type: "Feature",
          id: `pred-pt-${pred.id}`,
          geometry: { type: "Point", coordinates: [pred.lon, pred.lat] },
          properties: {
            id: pred.id,
            entity_id: pred.entity_id,
            legacy_obs_id: pred.legacy_obs_id ?? "",
            label: pred.label,
            status: pred.goal_status ?? "neutral",
            sigma_meters: pred.sigma_meters,
            goal_probability: pred.goal_probability,
          },
        },
      ];
    }),
  };
}

export function photoLocationFeatures(media: MediaItem[]): FeatureCollection<Point, { filename: string }> {
  return {
    type: "FeatureCollection",
    features: media.flatMap(({ id, lat, lon, filename }) => {
      if (lat === null || lon === null || !isMappable(lat, lon)) return [];
      return [{ type: "Feature", id, geometry: { type: "Point", coordinates: [lon, lat] }, properties: { filename } }];
    }),
  };
}

function ellipseRing(lon: number, lat: number, radius: number): [number, number][] | null {
  if (!Number.isFinite(radius) || radius <= 0) return null;
  const latRadius = radius / 111320;
  const lonScale = 111320 * Math.cos((lat * Math.PI) / 180);
  if (Math.abs(lat) + latRadius > 85.051129 || Math.abs(lonScale) < 1) return null;
  const lonRadius = radius / lonScale;
  if (Math.abs(lon) + lonRadius > 180) return null;
  return Array.from({ length: 49 }, (_, step) => {
    const angle = (step % 48) / 48 * Math.PI * 2;
    return [lon + Math.cos(angle) * lonRadius, lat + Math.sin(angle) * latRadius];
  });
}

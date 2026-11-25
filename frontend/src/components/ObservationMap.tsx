"use client";

import maplibregl, { Map as MapLibreMap, GeoJSONSource } from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Feature, FeatureCollection, Point } from "geojson";
import {
  isMappable,
  photoLocationFeatures,
  predictedPointFeatures,
  predictedUncertaintyFeatures,
  uncertaintyFeatures,
} from "@/lib/contours";
import { formatDateSpan } from "@/lib/time-filter";
import type { MediaItem, ObservationSummary, SpatialPrediction } from "@/lib/types";

const DEFAULT_CENTER: [number, number] = [9.814, 53.562];
const MAP_STYLE = process.env.NEXT_PUBLIC_MAP_STYLE_URL ?? "https://tiles.openfreemap.org/styles/liberty";
const SOURCE_ID = "observations";
const LAYER_ID = "observation-points";
const CONTOUR_SOURCE_ID = "uncertainty-contours";
const CONTOUR_68_LAYER_ID = "uncertainty-68";
const CONTOUR_95_LAYER_ID = "uncertainty-95";
const PRED_CONTOUR_SOURCE_ID = "predicted-contours";
const PRED_CONTOUR_95_LAYER_ID = "predicted-95";
const PRED_CONTOUR_68_LAYER_ID = "predicted-68";
const PRED_POINT_SOURCE_ID = "predicted-points";
const PRED_POINT_LAYER_ID = "predicted-point-layer";
const PHOTO_LOCATION_SOURCE_ID = "photo-locations";
const PHOTO_LOCATION_LAYER_ID = "photo-location-dots";
const USER_LOCATION_SOURCE_ID = "user-location";
const USER_LOCATION_LAYER_ID = "user-location-dot";

const PRED_STATUS_COLOR = [
  "match",
  ["get", "status"],
  "on_track",
  "#15803d",
  "at_risk",
  "#ca8a04",
  "broken",
  "#dc2626",
  "#2563eb",
] as maplibregl.ExpressionSpecification;

export function mapFeatures(observations: ObservationSummary[], selectedId: string | null = null): FeatureCollection<Point, { id: string; selected: boolean }> {
  return {
    type: "FeatureCollection",
    features: observations.flatMap((item) => {
      if (item.lat === null || item.lon === null || !isMappable(item.lat, item.lon)) return [];
      return [{ type: "Feature", id: item.id, geometry: { type: "Point", coordinates: [item.lon, item.lat] }, properties: { id: item.id, selected: item.id === selectedId } }];
    }),
  };
}

interface Props {
  observations: ObservationSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onZoomToVisible?: () => void;
  selectedMedia?: MediaItem[];
  draftLocation?: { lat: number | null; lon: number | null } | null;
  onDraftLocationPick?: (lat: number, lon: number) => void;
  spatialPredictions?: SpatialPrediction[];
}

export default function ObservationMap({ observations, selectedId, onSelect, onZoomToVisible, selectedMedia, draftLocation, onDraftLocationPick, spatialPredictions }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const handlerRef = useRef(onSelect);
  const draftMarkerRef = useRef<maplibregl.Marker | null>(null);
  const draftLocationRef = useRef(draftLocation);
  const onDraftPickRef = useRef(onDraftLocationPick);
  const [mapLoaded, setMapLoaded] = useState(false);
  const [mapError, setMapError] = useState(false);
  const [collisions, setCollisions] = useState<ObservationSummary[] | null>(null);
  const [showContours, setShowContours] = useState(true);
  const [showPredictions, setShowPredictions] = useState(true);
  const [rawLocationsFor, setRawLocationsFor] = useState<string | null>(null);
  const [locating, setLocating] = useState(false);
  const [locationNotice, setLocationNotice] = useState("");
  const lastUserCoordsRef = useRef<[number, number] | null>(null);
  const geojson = useMemo(() => mapFeatures(observations, selectedId), [observations, selectedId]);
  const contours = useMemo(() => uncertaintyFeatures(observations), [observations]);
  const predContours = useMemo(() => predictedUncertaintyFeatures(spatialPredictions ?? []), [spatialPredictions]);
  const predPoints = useMemo(() => predictedPointFeatures(spatialPredictions ?? []), [spatialPredictions]);
  const photoLocations = useMemo(() => photoLocationFeatures(selectedMedia ?? []), [selectedMedia]);
  const showPhotoLocations = selectedId !== null && rawLocationsFor === selectedId;
  const geojsonRef = useRef(geojson);
  const observationsRef = useRef(observations);
  geojsonRef.current = geojson;
  observationsRef.current = observations;
  handlerRef.current = onSelect;
  draftLocationRef.current = draftLocation;
  onDraftPickRef.current = onDraftLocationPick;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    let map: MapLibreMap;
    try {
      map = new maplibregl.Map({ container: containerRef.current, style: MAP_STYLE, center: DEFAULT_CENTER, zoom: 13, attributionControl: false });
      mapRef.current = map;
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
      map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
      map.on("error", (event) => {
        if (event.error) setMapError(true);
      });
      map.on("load", () => {
        if (!map.getSource(SOURCE_ID)) map.addSource(SOURCE_ID, { type: "geojson", data: mapFeatures([]) });
        map.addSource(CONTOUR_SOURCE_ID, { type: "geojson", data: uncertaintyFeatures([]) });
        map.addSource(PRED_CONTOUR_SOURCE_ID, { type: "geojson", data: predictedUncertaintyFeatures([]) });
        map.addSource(PRED_POINT_SOURCE_ID, { type: "geojson", data: predictedPointFeatures([]) });
        map.addSource(PHOTO_LOCATION_SOURCE_ID, { type: "geojson", data: photoLocationFeatures([]) });
        map.addSource(USER_LOCATION_SOURCE_ID, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        map.addLayer({ id: CONTOUR_95_LAYER_ID, type: "fill", source: CONTOUR_SOURCE_ID, filter: ["==", ["get", "level"], 95], layout: { visibility: showContours ? "visible" : "none" }, paint: { "fill-color": "#d9482b", "fill-opacity": 0.06, "fill-outline-color": "#d9482b" } });
        map.addLayer({ id: CONTOUR_68_LAYER_ID, type: "line", source: CONTOUR_SOURCE_ID, filter: ["==", ["get", "level"], 68], layout: { visibility: showContours ? "visible" : "none" }, paint: { "line-color": "#d9482b", "line-width": 1.25, "line-opacity": 0.55 } });
        map.addLayer({ id: PRED_CONTOUR_95_LAYER_ID, type: "fill", source: PRED_CONTOUR_SOURCE_ID, filter: ["==", ["get", "level"], 95], layout: { visibility: showPredictions ? "visible" : "none" }, paint: { "fill-color": PRED_STATUS_COLOR, "fill-opacity": 0.08 } });
        map.addLayer({ id: PRED_CONTOUR_68_LAYER_ID, type: "line", source: PRED_CONTOUR_SOURCE_ID, filter: ["==", ["get", "level"], 68], layout: { visibility: showPredictions ? "visible" : "none" }, paint: { "line-color": PRED_STATUS_COLOR, "line-width": 1.8, "line-dasharray": [4, 3], "line-opacity": 0.85 } });
        map.addLayer({ id: PHOTO_LOCATION_LAYER_ID, type: "circle", source: PHOTO_LOCATION_SOURCE_ID, paint: { "circle-radius": 4, "circle-color": "#1555a4", "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 } });
        map.addLayer({ id: LAYER_ID, type: "circle", source: SOURCE_ID, layout: { "circle-sort-key": ["case", ["get", "selected"], 1, 0] }, paint: { "circle-radius": ["case", ["get", "selected"], 8, 6], "circle-color": ["case", ["get", "selected"], "#d9482b", "#18181b"], "circle-stroke-color": "#ffffff", "circle-stroke-width": 2, "circle-opacity": 0.92 } });
        map.addLayer({ id: PRED_POINT_LAYER_ID, type: "circle", source: PRED_POINT_SOURCE_ID, layout: { visibility: showPredictions ? "visible" : "none" }, paint: { "circle-radius": 6, "circle-color": "#ffffff", "circle-stroke-color": PRED_STATUS_COLOR, "circle-stroke-width": 2.5, "circle-opacity": 0.96 } });
        map.addLayer({ id: USER_LOCATION_LAYER_ID, type: "circle", source: USER_LOCATION_SOURCE_ID, paint: { "circle-radius": 6, "circle-color": "#2563eb", "circle-stroke-color": "#ffffff", "circle-stroke-width": 2.5 } });
        map.on("click", LAYER_ID, (event) => {
          if (draftLocationRef.current) return;
          const features = map.queryRenderedFeatures(event.point, { layers: [LAYER_ID] });
          const ids = [...new Set(features.map((feature) => String(feature.properties?.id ?? feature.id)))];
          if (ids.length > 1) setCollisions(observationsRef.current.filter((item) => ids.includes(item.id)));
          else if (ids[0]) handlerRef.current(ids[0]);
        });
        map.on("click", PRED_POINT_LAYER_ID, (event) => {
          if (draftLocationRef.current) return;
          const legacyId = String(event.features?.[0]?.properties?.legacy_obs_id ?? "");
          if (legacyId) handlerRef.current(legacyId);
        });
        map.on("mousemove", PRED_POINT_LAYER_ID, (event) => {
          const pred = event.features?.[0];
          if (pred?.geometry.type !== "Point") return;
          const lbl = String(pred.properties?.label ?? "Prognose");
          const sig = Number(pred.properties?.sigma_meters ?? 0);
          const prob = pred.properties?.goal_probability;
          const pct = typeof prob === "number" ? ` · P=${Math.round(prob * 100)} %` : "";
          photoPopup.setLngLat(pred.geometry.coordinates as [number, number]).setText(`${lbl} (±${Math.round(sig)} m${pct})`).addTo(map);
        });
        map.on("mouseleave", PRED_POINT_LAYER_ID, () => photoPopup.remove());
        map.on("click", (event) => {
          if (!draftLocationRef.current) return;
          onDraftPickRef.current?.(event.lngLat.lat, event.lngLat.lng);
        });
        map.on("mouseenter", LAYER_ID, () => { if (!draftLocationRef.current) map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", LAYER_ID, () => { map.getCanvas().style.cursor = draftLocationRef.current ? "crosshair" : ""; });
        const photoPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
        map.on("mousemove", PHOTO_LOCATION_LAYER_ID, (event) => {
          const photo = event.features?.[0];
          if (photo?.geometry.type !== "Point") return;
          photoPopup.setLngLat(photo.geometry.coordinates as [number, number]).setText(String(photo.properties.filename)).addTo(map);
        });
        map.on("mouseleave", PHOTO_LOCATION_LAYER_ID, () => photoPopup.remove());
        map.on("remove", () => photoPopup.remove());
        setMapLoaded(true);
        map.resize();
        if (typeof navigator !== "undefined" && navigator.geolocation && window.isSecureContext) {
          requestGeoPosition(
            (lat, lon) => {
              lastUserCoordsRef.current = [lat, lon];
              updateUserLocation(map, lat, lon);
              fitAroundLocation(map, lat, lon, 250);
            },
            () => {
              if (geojsonRef.current.features.length) fitPoints(map, geojsonRef.current.features);
            }
          );
        } else if (geojsonRef.current.features.length) {
          fitPoints(map, geojsonRef.current.features);
        }
      });
    } catch {
      setMapError(true);
    }
    const observer = new ResizeObserver(() => mapRef.current?.resize());
    if (containerRef.current) observer.observe(containerRef.current);
    return () => {
      observer.disconnect();
      draftMarkerRef.current?.remove();
      draftMarkerRef.current = null;
      mapRef.current?.remove();
      mapRef.current = null;
    };
    // One map instance per mount; data and callbacks update independently.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    updateGeoSource(map, SOURCE_ID, geojson);
    updateGeoSource(map, CONTOUR_SOURCE_ID, contours);
    updateGeoSource(map, PRED_CONTOUR_SOURCE_ID, predContours);
    updateGeoSource(map, PRED_POINT_SOURCE_ID, predPoints);
  }, [mapLoaded, geojson, contours, predContours, predPoints]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    updateGeoSource(map, PHOTO_LOCATION_SOURCE_ID, showPhotoLocations ? photoLocations : photoLocationFeatures([]));
  }, [mapLoaded, photoLocations, showPhotoLocations]);

  const selectedFeature = geojson.features.find((feature) => feature.properties.selected);
  const selectedLat = selectedFeature?.geometry.coordinates[1];
  const selectedLon = selectedFeature?.geometry.coordinates[0];
  useEffect(() => {
    if (mapLoaded && selectedLat !== undefined && selectedLon !== undefined) mapRef.current?.easeTo({ center: [selectedLon, selectedLat], duration: 500 });
  }, [mapLoaded, selectedLat, selectedLon]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    map.getCanvas().style.cursor = draftLocation ? "crosshair" : "";
    if (!draftLocation || draftLocation.lat === null || draftLocation.lon === null || !Number.isFinite(draftLocation.lat) || !Number.isFinite(draftLocation.lon)) {
      draftMarkerRef.current?.remove();
      draftMarkerRef.current = null;
      return;
    }
    const lat = draftLocation.lat;
    const lon = draftLocation.lon;
    if (!draftMarkerRef.current) {
      const marker = new maplibregl.Marker({ draggable: true, color: "#b34b2f" }).setLngLat([lon, lat]).addTo(map);
      marker.on("dragend", () => {
        const pos = marker.getLngLat();
        onDraftPickRef.current?.(pos.lat, pos.lng);
      });
      draftMarkerRef.current = marker;
    } else {
      const current = draftMarkerRef.current.getLngLat();
      if (Math.abs(current.lat - lat) > 1e-6 || Math.abs(current.lng - lon) > 1e-6) {
        draftMarkerRef.current.setLngLat([lon, lat]);
      }
    }
  }, [draftLocation, mapLoaded]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    for (const layer of [CONTOUR_68_LAYER_ID, CONTOUR_95_LAYER_ID]) map.setLayoutProperty(layer, "visibility", showContours ? "visible" : "none");
  }, [mapLoaded, showContours]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    for (const layer of [PRED_CONTOUR_68_LAYER_ID, PRED_CONTOUR_95_LAYER_ID, PRED_POINT_LAYER_ID]) map.setLayoutProperty(layer, "visibility", showPredictions ? "visible" : "none");
  }, [mapLoaded, showPredictions]);

  const selectedObservation = useMemo(
    () => (selectedId ? observations.find((item) => item.id === selectedId) ?? null : null),
    [observations, selectedId]
  );
  const hasSelectedCoords = selectedLat !== undefined && selectedLon !== undefined;

  const zoomToVisible = useCallback(() => {
    const map = mapRef.current;
    if (map) {
      if (hasSelectedCoords && selectedObservation) {
        const radius = Math.max(60, (selectedObservation.sigma_meters ?? 15) * 3.5);
        fitAroundLocation(map, selectedObservation.lat!, selectedObservation.lon!, radius);
      } else {
        fitPoints(map, geojson.features);
      }
    }
    onZoomToVisible?.();
  }, [geojson, hasSelectedCoords, selectedObservation, onZoomToVisible]);

  const zoomToMe = useCallback(() => {
    const map = mapRef.current;
    if (!map) return;
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      setLocationNotice("Dieser Browser unterstützt keine Standortabfrage.");
      return;
    }
    if (typeof window !== "undefined" && !window.isSecureContext) {
      setLocationNotice("Standort benötigt HTTPS (z. B. Port 3443) oder localhost.");
      return;
    }
    setLocationNotice("");
    if (lastUserCoordsRef.current) {
      const [cachedLat, cachedLon] = lastUserCoordsRef.current;
      updateUserLocation(map, cachedLat, cachedLon);
      fitAroundLocation(map, cachedLat, cachedLon, 250);
    }
    setLocating(true);
    requestGeoPosition(
      (lat, lon) => {
        lastUserCoordsRef.current = [lat, lon];
        updateUserLocation(map, lat, lon);
        fitAroundLocation(map, lat, lon, 250);
        setLocationNotice("");
        setLocating(false);
      },
      (reason) => {
        setLocating(false);
        if (!lastUserCoordsRef.current) setLocationNotice(reason);
      }
    );
  }, []);

  return <section className="map-shell" aria-label="Beobachtungen auf der Karte">
    <div ref={containerRef} className={`map-canvas ${mapError ? "map-canvas--error" : ""}`} />
    <div className="map-credit">Karte: OpenFreeMap · © OpenStreetMap</div>
    <div className="map-overlays" aria-label="Kartenebenen"><label><input type="checkbox" checked={showContours} onChange={(event) => setShowContours(event.target.checked)} /> Kalman-Unsicherheit 68 / 95 %</label>{predPoints.features.length > 0 && <label><input type="checkbox" checked={showPredictions} onChange={(event) => setShowPredictions(event.target.checked)} /> <i className="prediction-key" aria-hidden="true" /> Zukunfts-Prognosen &amp; Habitate ({predPoints.features.length})</label>}{selectedId && photoLocations.features.length > 0 && <label><input type="checkbox" checked={showPhotoLocations} onChange={(event) => setRawLocationsFor(event.target.checked ? selectedId : null)} /> <i className="photo-location-key" aria-hidden="true" /> Foto-GPS anzeigen</label>}</div>
    <div className="map-actions">
      <button type="button" className="map-zoom-button" onClick={zoomToVisible}>{hasSelectedCoords ? "Auf Fund zoomen" : "Auf Funde zoomen"} <span aria-hidden="true">↗</span></button>
      <button type="button" className="map-zoom-button" disabled={locating} onClick={zoomToMe}>{locating ? "Standort …" : "Mein Standort (± 250 m)"}</button>
      {locationNotice && <div className="map-location-notice" role="status"><span>{locationNotice}</span><button type="button" aria-label="Hinweis schließen" onClick={() => setLocationNotice("")}>×</button></div>}
    </div>
    {draftLocation && <div className="map-edit-banner" role="status">
      <span>{draftLocation.lat === null ? "Klicke auf die Karte, um den Aufnahmeort des Fotos zu setzen" : "Foto-Marker verschieben oder neu klicken"}</span>
      <button type="button" onClick={() => { const center = mapRef.current?.getCenter(); if (center) onDraftPickRef.current?.(center.lat, center.lng); }}>In Kartenmitte setzen</button>
    </div>}
    {mapError && <div className="map-error" role="status"><strong>Karte gerade nicht erreichbar</strong><span>Die Liste und Funddetails bleiben weiterhin verfügbar.</span></div>}
    {collisions && <div className="collision-popover" role="dialog" aria-label="Funde an diesem Ort">
      <div className="collision-head"><strong>{collisions.length} Funde an diesem Ort</strong><button type="button" aria-label="Auswahlliste schließen" onClick={() => setCollisions(null)}>×</button></div>
      {collisions.map((item) => <button className="collision-item" type="button" key={item.id} onClick={() => { handlerRef.current(item.id); setCollisions(null); }}>{item.title}<span>{formatDateSpan(item.first_observed_at, item.last_observed_at)}</span></button>)}
    </div>}
  </section>;
}

function updateGeoSource(map: MapLibreMap, id: string, data: FeatureCollection) {
  (map.getSource(id) as GeoJSONSource | undefined)?.setData(data);
}

function updateUserLocation(map: MapLibreMap, lat: number, lon: number) {
  updateGeoSource(map, USER_LOCATION_SOURCE_ID, {
    type: "FeatureCollection",
    features: [{ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: {} }],
  });
}

function requestGeoPosition(onSuccess: (lat: number, lon: number) => void, onError: (reason: string) => void) {
  navigator.geolocation.getCurrentPosition(
    (pos) => onSuccess(pos.coords.latitude, pos.coords.longitude),
    (err) => {
      if (err.code === 1) {
        onError("Standortfreigabe im Browser abgelehnt oder blockiert.");
        return;
      }
      // Retry with standard network/Wi-Fi accuracy when high-accuracy GPS fails or times out indoors.
      navigator.geolocation.getCurrentPosition(
        (pos) => onSuccess(pos.coords.latitude, pos.coords.longitude),
        (fallbackErr) => {
          onError(
            fallbackErr.code === 1
              ? "Standortfreigabe im Browser abgelehnt oder blockiert."
              : "Gerätestandort konnte nicht ermittelt werden (Ortung/WLAN prüfen)."
          );
        },
        { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 }
      );
    },
    { enableHighAccuracy: true, timeout: 6000, maximumAge: 60000 }
  );
}

function fitAroundLocation(map: MapLibreMap, lat: number, lon: number, radiusMeters = 1000) {
  map.stop();
  const dLat = radiusMeters / 111320;
  const dLon = radiusMeters / (111320 * Math.max(0.1, Math.cos((lat * Math.PI) / 180)));
  map.fitBounds(
    [[lon - dLon, lat - dLat], [lon + dLon, lat + dLat]],
    { padding: 24, duration: 500 }
  );
}

function fitPoints(map: MapLibreMap, features: Feature<Point, unknown>[]) {
  if (features.length === 0) {
    map.flyTo({ center: DEFAULT_CENTER, zoom: 5, duration: 500 });
    return;
  }
  if (features.length === 1) {
    map.flyTo({ center: features[0].geometry.coordinates as [number, number], zoom: 11, duration: 600 });
    return;
  }
  const bounds = new maplibregl.LngLatBounds();
  features.forEach((feature) => bounds.extend(feature.geometry.coordinates as [number, number]));
  map.fitBounds(bounds, { padding: 70, maxZoom: 11, duration: 500 });
}

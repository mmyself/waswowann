"use client";

import maplibregl, { Map as MapLibreMap } from "maplibre-gl";
import { useEffect, useRef } from "react";

const DEFAULT_CENTER: [number, number] = [9.814, 53.562];
const MAP_STYLE = process.env.NEXT_PUBLIC_MAP_STYLE_URL ?? "https://tiles.openfreemap.org/styles/liberty";

interface Props {
  lat: number | null;
  lon: number | null;
  onPick: (lat: number, lon: number) => void;
}

export default function MiniLocationPicker({ lat, lon, onPick }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const onPickRef = useRef(onPick);
  onPickRef.current = onPick;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const center: [number, number] = lat !== null && lon !== null ? [lon, lat] : DEFAULT_CENTER;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      center,
      zoom: lat !== null && lon !== null ? 15 : 13,
      attributionControl: false,
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.getCanvas().style.cursor = "crosshair";
    map.on("click", (event) => {
      onPickRef.current(event.lngLat.lat, event.lngLat.lng);
    });
    if ((lat === null || lon === null) && typeof navigator !== "undefined" && navigator.geolocation && window.isSecureContext) {
      navigator.geolocation.getCurrentPosition(
        (pos) => map.easeTo({ center: [pos.coords.longitude, pos.coords.latitude], zoom: 15, duration: 400 }),
        () => undefined,
        { enableHighAccuracy: false, timeout: 6000, maximumAge: 120000 }
      );
    }
    return () => {
      markerRef.current?.remove();
      markerRef.current = null;
      mapRef.current?.remove();
      mapRef.current = null;
    };
    // Initialize embedded picker map once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (lat === null || lon === null || !Number.isFinite(lat) || !Number.isFinite(lon)) {
      markerRef.current?.remove();
      markerRef.current = null;
      return;
    }
    if (!markerRef.current) {
      const marker = new maplibregl.Marker({ draggable: true, color: "#b34b2f" }).setLngLat([lon, lat]).addTo(map);
      marker.on("dragend", () => {
        const pos = marker.getLngLat();
        onPickRef.current(pos.lat, pos.lng);
      });
      markerRef.current = marker;
    } else {
      const current = markerRef.current.getLngLat();
      if (Math.abs(current.lat - lat) > 1e-6 || Math.abs(current.lng - lon) > 1e-6) {
        markerRef.current.setLngLat([lon, lat]);
      }
    }
  }, [lat, lon]);

  return <div className="mini-picker-wrap">
    <div ref={containerRef} className="mini-picker-map" aria-label="Karte zur Standortauswahl" />
    <p className="location-picker-hint">Klicke in die Karte oder ziehe den Marker (Drag & Drop) an den Fundort.</p>
  </div>;
}

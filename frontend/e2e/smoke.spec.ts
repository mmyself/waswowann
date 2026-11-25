import { expect, test } from "@playwright/test";
import type { MediaItem, ObservationDetail, ObservationSummary } from "../src/lib/types";

const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1sAAAAASUVORK5CYII=", "base64");

function photo(id: string, day: number, lat = 53.5501): MediaItem {
  return {
    id, filename: `${id}.jpg`, source: "local", width: 1200, height: 800,
    observed_at_ms: Date.UTC(2026, 10, day, 10, 20, 30), sigma_seconds: 1, time_source: "exif",
    lat, lon: 9.9901, sigma_meters: 10, location_source: "exif", exif: {},
    image_url: `/api/media/${id}/image`, thumbnail_url: `/api/media/${id}/thumbnail`,
  };
}

function observation(id: string, title: string, media: MediaItem[]): ObservationDetail {
  media.sort((a, b) => (a.observed_at_ms ?? Infinity) - (b.observed_at_ms ?? Infinity));
  const first = media.find((item) => item.observed_at_ms !== null)?.observed_at_ms;
  const last = media.filter((item) => item.observed_at_ms !== null).at(-1)?.observed_at_ms;
  const locations = media.filter((item) => item.lat !== null);
  const lat = locations.length ? locations.reduce((total, item) => total + item.lat!, 0) / locations.length : null;
  const lon = locations[0]?.lon ?? null;
  const sigma_meters = locations.length ? 10 / Math.sqrt(locations.length) : null;
  const observed_at = last == null ? null : new Date(last).toISOString();
  const stamp = new Date().toISOString();
  return {
    id, title, category: "Pilz", environment: "outdoor", description: "", lat, lon, sigma_meters, observed_at, sigma_seconds: 1,
    first_observed_at: first == null ? null : new Date(first).toISOString(), last_observed_at: observed_at,
    media_count: media.length, location_measurement_count: locations.length, created_at: stamp, updated_at: stamp,
    thumbnail_url: media[0]?.thumbnail_url ?? null, source: media[0]?.source ?? null, media,
    space_time: { lat, lon, sigma_meters, observed_at, sigma_seconds: 1, location_source: "exif", time_source: "exif" },
    gemini: { source: "placeholder", status: "not_requested", text: "", confidence: null, tags: [] },
  };
}

function summary({ description, space_time, media, gemini, created_at, updated_at, ...rest }: ObservationDetail): ObservationSummary {
  return rest;
}

test.beforeEach(async ({ page }) => {
  await page.route("**/styles/liberty", (route) => route.fulfill({ json: { version: 8, sources: {}, layers: [{ id: "ground", type: "background", paint: { "background-color": "#ecebe4" } }] } }));
});

test("adds, edits, deletes and merges photos in the selected object's timeline", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let main = observation("object-1", "Igel-Stachelbart", [photo("later", 22, 53.5502), photo("early", 15)]);
  const nearby = observation("object-2", "Pilz am Stamm", [photo("nearby", 19)]);
  let merged = false;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/config") return route.fulfill({ json: { google_photos_enabled: false, local_import_enabled: false, demo_enabled: false } });
    if (path.match(/\/(image|thumbnail)$/)) return route.fulfill({ contentType: "image/png", body: pixel });
    if (path === "/api/observations") {
      const items = (merged ? [main] : [main, nearby]).map(summary);
      return route.fulfill({ json: { items, total: items.length, located_count: items.length, undated_count: 0, time_extent: { min: main.first_observed_at, max: main.last_observed_at } } });
    }
    if (path === "/api/uploads") {
      expect(request.postData()).toContain('name="target_observation_id"');
      expect(request.postData()).toContain(main.id);
      main = observation(main.id, main.title, [...main.media, photo("added", 18, 53.5503)]);
      return route.fulfill({ json: { items: [{ filename: "added.jpg", observation_id: main.id, media_id: "added", result: "imported" }] } });
    }
    if (path === "/api/media/added" && request.method() === "PATCH") {
      const patch = request.postDataJSON();
      expect(patch).toEqual({ lat: 53.5506, lon: 9.9901, observed_at_ms: Date.UTC(2026, 10, 16, 10, 20, 30), sigma_meters: 7 });
      main = observation(main.id, main.title, main.media.map((item) => item.id === "added" ? { ...item, ...patch, location_source: "manual", time_source: "manual" } : item));
      return route.fulfill({ json: main });
    }
    if (path === "/api/media/added" && request.method() === "DELETE") {
      main = observation(main.id, main.title, main.media.filter((item) => item.id !== "added"));
      return route.fulfill({ json: main });
    }
    if (path === `/api/observations/${main.id}/merge-candidates`) {
      return route.fulfill({ json: { items: merged ? [] : [{ observation_id: nearby.id, title: nearby.title, category: nearby.category, observed_at: nearby.observed_at, thumbnail_url: nearby.thumbnail_url, distance_meters: 2.5, media_count: nearby.media_count }] } });
    }
    if (path === `/api/observations/${main.id}/merge`) {
      expect(request.postDataJSON()).toEqual({ source_observation_ids: [nearby.id] });
      main = observation(main.id, main.title, [...main.media, ...nearby.media]);
      merged = true;
      return route.fulfill({ json: main });
    }
    if (path === `/api/observations/${main.id}`) return route.fulfill({ json: main });
    return route.fulfill({ status: 404, json: {} });
  });

  await page.goto("/");
  const entry = page.getByRole("button", { name: /Igel-Stachelbart/ });
  await expect(entry).toContainText("2 Fotos");
  await expect(entry).toContainText(/15\.–22\. Nov/);
  await entry.click();
  const object = page.getByRole("region", { name: "Kalman-Fundort" });
  const selected = page.getByRole("region", { name: "Messwerte des ausgewählten Fotos" });
  const strip = page.getByRole("group", { name: "Fotos in zeitlicher Reihenfolge" });
  await expect(object).toContainText("2 GPS-Messungen");
  await expect(object).toContainText("±7,1 m");
  await expect(strip.getByRole("button")).toHaveText(["15. Nov", "22. Nov"]);
  const raw = page.getByRole("checkbox", { name: "Foto-GPS anzeigen" });
  await expect(raw).not.toBeChecked();
  await raw.check();
  await expect(raw).toBeChecked();
  await expect(page.getByText("Fusionen", { exact: true })).toHaveCount(0);
  await page.getByLabel("Weitere Fotos zu diesem Fund hinzufügen").setInputFiles({ name: "added.jpg", mimeType: "image/jpeg", buffer: pixel });
  await expect(strip.getByRole("button")).toHaveText(["15. Nov", "18. Nov", "22. Nov"]);
  await expect(strip.getByRole("button", { name: /added.jpg/ })).toHaveAttribute("aria-pressed", "true");
  await expect(object).toContainText("3 GPS-Messungen");
  await expect(selected).toContainText("53.55030");
  await page.getByRole("button", { name: "Foto-Messwerte bearbeiten" }).click();
  await expect(selected.getByLabel("Breitengrad")).toHaveValue("53.5503");
  await selected.getByLabel("Breitengrad").fill("53.5506");
  await selected.getByLabel(/Aufnahmezeit/).fill("2026-11-16T11:20:30");
  await selected.getByLabel("Ort ± Meter").fill("7");
  await page.getByRole("button", { name: "Foto-Messwerte speichern" }).click();
  await expect(strip.getByRole("button")).toHaveText(["15. Nov", "16. Nov", "22. Nov"]);
  await expect(selected).toContainText("53.55060");
  await page.getByRole("button", { name: "Foto löschen" }).click();
  await page.getByRole("button", { name: "Endgültig löschen" }).click();
  await expect(object).toContainText("2 GPS-Messungen");
  await expect(strip.getByRole("button")).toHaveCount(2);
  await page.getByRole("button", { name: "Zusammenführen", exact: true }).click();
  await page.getByRole("button", { name: "Funde zusammenführen" }).click();
  await expect(strip.getByRole("button")).toHaveText(["15. Nov", "19. Nov", "22. Nov"]);
  await expect(object).toContainText("3 GPS-Messungen");
  await expect(page.getByRole("button", { name: "Zurück zur Liste (1)" })).toBeVisible();
  await page.locator(".side-strand").screenshot({ path: testInfo.outputPath("timeline.png") });
  await page.getByRole("button", { name: /Zurück zur Liste/ }).click();
  await expect(page.getByRole("button", { name: /Pilz am Stamm/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Zeitfenster" }).click();
  await page.getByLabel("Von Datum und Uhrzeit").fill("2026-11-17T11:20");
  await page.getByLabel("Bis Datum und Uhrzeit").fill("2026-11-18T11:20");
  await expect(entry).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await entry.click();
  await expect(strip.getByRole("button")).toHaveCount(3);
  expect(await page.locator("body").evaluate((body) => body.scrollWidth <= window.innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test("shows the add-photo action on a photo-less fund in a narrow mobile layout", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  const empty = observation("empty", "Ohne Fotos", []);
  await page.route("**/api/config", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/observations", (route) => route.fulfill({ json: { items: [summary(empty)], total: 1, located_count: 0, undated_count: 1, time_extent: null } }));
  await page.route("**/api/observations/empty", (route) => route.fulfill({ json: empty }));
  await page.goto("/?observation=empty");
  await expect(page.getByRole("heading", { name: "Ohne Fotos" })).toBeVisible();
  await expect(page.getByRole("button", { name: "+ Foto hinzufügen" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Foto löschen" })).toHaveCount(0);
  expect(await page.locator("body").evaluate((body) => body.scrollWidth <= window.innerWidth)).toBe(true);
  await page.goto("/upload");
  await expect(page.getByRole("heading", { name: /Ein Augenblick/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /Fotos auswählen/i })).toBeVisible();
  expect(await page.locator("body").evaluate((body) => body.scrollWidth <= window.innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

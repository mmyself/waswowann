from __future__ import annotations

import io
import json
import zipfile

import pytest
from PIL import Image

import app.routes.analysis as analysis_route
import app.routes.observations as observations_route
import app.routes.uploads as uploads_route
from app.db import db, migrate

from app.services.gaussian_fusion import fuse_media_measurements
from app.services.gemini_client import _parse
from app.services.upload_ingest import unpack_uploads


def _jpeg() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 30), (30, 90, 40)).save(output, "JPEG")
    return output.getvalue()


def test_upload_endpoint_parses_browser_location(client, monkeypatch):
    browser_location = {"lat": 52.5, "lon": 13.4, "accuracy_meters": 25, "captured_at": "2026-11-15T12:00:00Z"}
    imported = []
    monkeypatch.setattr(uploads_route, "unpack_uploads", lambda files: [("photo.jpg", b"image", None)])

    def mock_import_upload(filename, data, config, location, timezone_name):
        imported.append(location)
        return {"filename": filename, "result": "imported"}

    monkeypatch.setattr(uploads_route, "import_upload", mock_import_upload)
    response = client.post(
        "/api/uploads",
        files={"files": ("photo.jpg", b"image", "image/jpeg")},
        data={"browser_location": json.dumps(browser_location)},
        headers={"Origin": "http://localhost:3000"},
    )

    assert response.status_code == 202
    assert imported == [browser_location]


def _isolated_routes(config, monkeypatch):
    migrate(config)

    def isolated_db():
        return db(config)

    monkeypatch.setattr(analysis_route, "db", isolated_db)
    monkeypatch.setattr(observations_route, "db", isolated_db)


def _seed_observation(conn, observation_id, *, note_id="note"):
    conn.execute(
        "INSERT INTO observations(id,category,title,description,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?)",
        (observation_id, "Pilz", observation_id, "Beschreibung", 1, 1),
    )
    conn.execute(
        """INSERT INTO space_time_gauss
           (obs_id) VALUES(?)""",
        (observation_id,),
    )
    conn.execute(
        """INSERT INTO gemini_notes
           (id,obs_id,source,status,raw_text,structured_json,created_at_ms,parent_note_id,question_text,provider,model)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (note_id, observation_id, "gemini_api", "ready", "Analyse", json.dumps({"title": "Steinpilz", "confidence": 0.9}), 2, None, None, "google", "gemini-test"),
    )


def test_analysis_follow_ups_create_distinct_notes_and_jobs(config, monkeypatch):
    _isolated_routes(config, monkeypatch)
    monkeypatch.setattr(analysis_route.runner, "notify", lambda: None)
    with db(config) as conn:
        _seed_observation(conn, "obs")

    body = analysis_route.Request(question="Welche Merkmale?", parent_note_id="note")
    first = analysis_route.request_analysis("obs", body)
    second = analysis_route.request_analysis("obs", body)

    assert first["status"] == second["status"] == "queued"
    assert first["note_id"] != second["note_id"]
    with db(config) as conn:
        notes = conn.execute(
            "SELECT id,parent_note_id,question_text FROM gemini_notes WHERE obs_id='obs' AND source='gemini_api' ORDER BY id"
        ).fetchall()
        jobs = conn.execute(
            "SELECT note_id,input_key,status FROM processing_jobs WHERE obs_id='obs' AND kind='gemini' ORDER BY note_id"
        ).fetchall()
    assert {row["id"] for row in notes if row["id"] != "note"} == {first["note_id"], second["note_id"]}
    assert all(row["parent_note_id"] == "note" and row["question_text"] == "Welche Merkmale?" for row in notes if row["id"] != "note")
    assert len(jobs) == 2
    assert {row["note_id"] for row in jobs} == {first["note_id"], second["note_id"]}


def test_observation_detail_exposes_frontend_analysis_contract(config, monkeypatch):
    _isolated_routes(config, monkeypatch)
    with db(config) as conn:
        _seed_observation(conn, "obs")
        conn.execute(
            "UPDATE gemini_notes SET structured_json=?,question_text=? WHERE id='note'",
            (json.dumps({"title": "Steinpilz", "confidence": 0.9, "tags": ["Pilz"]}), "Welche Merkmale?"),
        )

    detail = observations_route.get_observation("obs")

    assert detail["gemini"]["result"]["title"] == "Steinpilz"
    assert detail["gemini"]["history"][0]["note_id"] == "note"
    assert detail["gemini"]["history"][0]["question_text"] == "Welche Merkmale?"
    assert detail["space_time"]["location_measurement_count"] == 0
    assert detail["media_count"] == 0



def test_upload_zip_is_expanded_in_memory_and_rejects_traversal():
    image = _jpeg()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("folder/photo.jpg", image)
    items = unpack_uploads([("photos.zip", output.getvalue())])
    assert items == [("photo.jpg", image, None)]

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../photo.jpg", image)
    try:
        unpack_uploads([("photos.zip", unsafe.getvalue())])
        assert False, "ZIP traversal must be rejected"
    except ValueError as exc:
        assert str(exc) == "UNSAFE_ZIP"


def test_gaussian_fusion_weights_gps_independently_of_time_and_wraps_dateline(config):
    def measurement(lat, lon, sigma, ts=None):
        return dict(lat=lat, lon=lon, sigma_meters=sigma, location_source="exif",
                    observed_at_ms=ts, sigma_seconds=60.0, time_source="exif")

    rows = [measurement(52.0, 179.99, 10.0), measurement(52.001, -179.99, 20.0),
            measurement(None, None, None, 1_800_000_000_000)]
    fused = fuse_media_measurements(rows, config=config)
    assert fused == fuse_media_measurements(reversed(rows), config=config)
    assert fused["location_measurement_count"] == 2
    assert fused["lat"] == pytest.approx(52.0002)
    assert fused["lon"] == pytest.approx(179.994, abs=1e-9)
    # Points are ~1.375 km apart while sigmas claim 10m/20m, so Birge scaling inflates uncertainty cleanly.
    assert fused["sigma_meters"] > 300.0
    assert fused["sigma_meters"] > (1 / 10**2 + 1 / 20**2) ** -0.5
    assert fused["first_observed_at_ms"] == fused["last_observed_at_ms"] == 1_800_000_000_000


def test_gemini_analysis_schema_validation():
    result = _parse('{"title":"Steinpilz","category":"Pilz","description":"Fruchtkörper","confidence":0.8,"tags":[],"alternatives":[],"questions":[],"warnings":[]}')
    assert result.title == "Steinpilz"
    assert result.confidence == 0.8
    try:
        _parse('{"confidence":1.2}')
        assert False, "Out-of-range confidence must be rejected"
    except ValueError as exc:
        assert str(exc) == "ANALYSIS_SCHEMA_INVALID"


def test_exif_ingest_initializes_sigmas_and_reads_positioning_error(config):
    from app.services.photo_ingest import ingest_image

    migrate(config)
    img = Image.new("RGB", (40, 30), (30, 90, 40))
    exif = Image.Exif()
    exif[306] = "2026:05:14 08:15:00"
    exif_ifd = exif.get_ifd(0x8825)
    exif_ifd[1] = "N"
    exif_ifd[2] = (53.0, 33.0, 0.0)
    exif_ifd[3] = "E"
    exif_ifd[4] = (9.0, 48.0, 0.0)
    exif_ifd[31] = 12.5
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())

    media_id, status = ingest_image(
        source="local", external_id="exif-sigmas", filename="exif.jpg", data=buf.getvalue(), config=config
    )
    assert status == "imported" and media_id
    with db(config) as conn:
        st = conn.execute("SELECT sigma_meters, sigma_seconds, location_source, time_source FROM media WHERE id=?", (media_id,)).fetchone()
    assert st["location_source"] == "exif"
    assert st["time_source"] == "exif"
    assert st["sigma_meters"] == 12.5
    assert st["sigma_seconds"] == config.fusion_default_sigma_seconds


def test_circular_time_patterns_across_midnight_and_new_year(config):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.import_timezone)
    dates = (datetime(2025, 12, 31, 23, 55, tzinfo=tz), datetime(2026, 1, 1, 0, 5, tzinfo=tz))
    times = [int(date.timestamp() * 1000) for date in dates]
    rows = [dict(lat=None, lon=None, sigma_meters=None, location_source="unknown",
                 observed_at_ms=ts, sigma_seconds=60.0, time_source="exif") for ts in times]
    fused = fuse_media_measurements(rows, config=config)

    assert fused["first_observed_at_ms"] == times[0]
    assert fused["last_observed_at_ms"] == times[1]
    assert fused["time_of_day"]["mean_label"] == "00:00"
    assert fused["time_of_day"]["concentration"] > 0.99
    assert fused["season"]["mean_label"] == "1. Jan"
    assert fused["season"]["sigma_days"] < 1


def test_gemini_analysis_parses_uncertainty_estimates():
    result = _parse(
        '[{"title":"Igel-Stachelbart","category":"Pilz","description":"Beutel","confidence":0.7,'
        '"tags":[],"alternatives":[],"questions":[],"warnings":[],'
        '"estimated_sigma_meters":35,"estimated_sigma_seconds":120,"uncertainty_rationale":"Innenraum"}]'
    )
    assert result.estimated_sigma_meters == 35.0
    assert result.estimated_sigma_seconds == 120.0
    assert result.uncertainty_rationale == "Innenraum"



def test_follow_up_correction_updates_observation_and_environment(config, monkeypatch):
    import app.services.processing_jobs as jobs_service
    from app.services.gemini_client import AnalysisResult
    from app.schemas import ObservationPatch

    _isolated_routes(config, monkeypatch)
    monkeypatch.setattr(jobs_service, "db", lambda: db(config))
    monkeypatch.setattr(jobs_service, "settings", config)
    monkeypatch.setattr(jobs_service.runner, "notify", lambda: None)

    media_dir = config.photo_storage_dir / "media-1"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "image.jpg").write_bytes(_jpeg())

    with db(config) as conn:
        _seed_observation(conn, "obs", note_id="note-root")
        conn.execute(
            "INSERT INTO media(id,obs_id,source,external_id,original_name,local_path,thumbnail_path,mime_type,width,height,sha256,exif_json,created_at_ms) "
            "VALUES('media-1','obs','local','ext-1','a.jpg','media-1/image.jpg','media-1/image.jpg','image/jpeg',40,30,'h1','{}',1)"
        )
        conn.execute(
            "INSERT INTO gemini_notes(id,obs_id,source,status,raw_text,structured_json,created_at_ms,parent_note_id,question_text) "
            "VALUES('note-follow','obs','gemini_api','queued','','{}',10,'note-root','Das ist ein Kräuterseitling in meiner Wohnung')"
        )

    monkeypatch.setattr(
        jobs_service,
        "analyze",
        lambda *args, **kwargs: AnalysisResult(
            title="Kräuterseitling",
            category="Pilz",
            description="Korrigiert auf Kräuterseitling in Innenraumkultur.",
            scientific_name="Pleurotus eryngii",
            confidence=0.88,
            tags=["Kräuterseitling"],
            alternatives=[],
            questions=[],
            warnings=[],
            raw_text="{}",
            environment="indoor",
            apply_correction=True,
        ),
    )

    jobs_service.JobRunner()._analysis({"id": "job-1", "obs_id": "obs", "note_id": "note-follow"})

    updated = observations_route.get_observation("obs")
    assert updated["title"] == "Kräuterseitling"
    assert updated["environment"] == "indoor"
    assert "weather" not in updated

    patched = observations_route.patch_observation("obs", ObservationPatch(environment="outdoor"))
    assert patched["environment"] == "outdoor"

    # Parsing indoor/apply_correction from raw JSON works cleanly.
    parsed = _parse(
        '{"title":"Igel-Stachelbart","category":"Pilz","description":"ok","environment":"indoor","apply_correction":true}'
    )
    assert parsed.environment == "indoor"
    assert parsed.apply_correction is True


def _jpeg_with_gps(color: tuple[int, int, int], lat_deg: float, lon_deg: float) -> bytes:
    img = Image.new("RGB", (40, 30), color)
    exif = Image.Exif()
    exif[306] = "2026:11:16 09:00:00"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (int(lat_deg), int((lat_deg % 1) * 60), round(((lat_deg * 60) % 1) * 60, 2))
    gps[3] = "E"
    gps[4] = (int(lon_deg), int((lon_deg % 1) * 60), round(((lon_deg * 60) % 1) * 60, 2))
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def test_multi_upload_same_location_groups_media_and_analyzes_all_images(client, config, monkeypatch):
    import app.db as dm
    import app.services.processing_jobs as jobs_service
    from app.services.gemini_client import AnalysisResult

    _isolated_routes(config, monkeypatch)
    monkeypatch.setattr(dm, "settings", config)
    monkeypatch.setattr(uploads_route, "settings", config)
    monkeypatch.setattr(jobs_service, "db", lambda: db(config))
    monkeypatch.setattr(jobs_service, "settings", config)
    monkeypatch.setattr(jobs_service.runner, "notify", lambda: None)

    first_jpg = _jpeg_with_gps((20, 80, 30), 53.5582, 9.8083)
    second_jpg = _jpeg_with_gps((90, 40, 20), 53.55821, 9.80831)
    far_jpg = _jpeg_with_gps((10, 10, 120), 53.6200, 9.9000)

    response = client.post(
        "/api/uploads",
        files=[
            ("files[]", ("a.jpg", first_jpg, "image/jpeg")),
            ("files[]", ("b.jpg", second_jpg, "image/jpeg")),
            ("files[]", ("far.jpg", far_jpg, "image/jpeg")),
        ],
        data={"group_same_location": "true"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.status_code == 202
    items = response.json()["items"]
    assert len(items) == 3
    assert items[0]["observation_id"] == items[1]["observation_id"]
    assert items[2]["observation_id"] != items[0]["observation_id"]

    grouped_id = items[0]["observation_id"]
    # Attach a 3rd photo directly to the existing observation
    third_jpg = _jpeg_with_gps((200, 180, 40), 53.55822, 9.80832)
    attach_res = client.post(
        "/api/uploads",
        files=[("files[]", ("c.jpg", third_jpg, "image/jpeg"))],
        data={"target_observation_id": grouped_id},
        headers={"Origin": "http://localhost:3000"},
    )
    assert attach_res.status_code == 202
    assert attach_res.json()["items"][0]["observation_id"] == grouped_id

    detail = observations_route.get_observation(grouped_id)
    assert len(detail["media"]) == 3
    # Kalman refinement across multiple GPS shots shrinks sigma_meters below the initial default
    assert detail["space_time"]["sigma_meters"] < config.fusion_default_sigma_meters

    listing = observations_route.list_observations()["items"]
    grouped_summary = next(row for row in listing if row["id"] == grouped_id)
    assert grouped_summary["media_count"] == 3

    seen_image_counts = []
    monkeypatch.setattr(
        jobs_service,
        "analyze",
        lambda *args, **kwargs: (
            seen_image_counts.append(len(kwargs.get("images") or []))
            or AnalysisResult(
                title="Igel-Stachelbart",
                category="Pilz",
                description="Mehrperspektivische Bestimmung",
                scientific_name="Hericium erinaceus",
                confidence=0.9,
                tags=[],
                alternatives=[],
                questions=[],
                warnings=[],
                raw_text="{}",
            )
        ),
    )
    note_id = detail["gemini"]["note_id"]
    jobs_service.JobRunner()._analysis({"id": "job-multi", "obs_id": grouped_id, "note_id": note_id})
    assert seen_image_counts == [3]



def test_google_photos_zip_reads_sidecars_and_queues_individual_gemini_jobs(client, config, monkeypatch):
    import app.db as dm
    import app.services.processing_jobs as jobs_service

    _isolated_routes(config, monkeypatch)
    monkeypatch.setattr(dm, "settings", config)
    monkeypatch.setattr(uploads_route, "settings", config)
    monkeypatch.setattr(jobs_service, "db", lambda: db(config))
    monkeypatch.setattr(jobs_service.runner, "notify", lambda: None)

    img_a = _jpeg()
    img_b_buf = io.BytesIO()
    Image.new("RGB", (40, 30), (140, 20, 40)).save(img_b_buf, "JPEG")
    img_b = img_b_buf.getvalue()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Takeout/Google Fotos/A.jpg", img_a)
        archive.writestr(
            "Takeout/Google Fotos/A.jpg.supplemental-metadata.json",
            json.dumps({
                "title": "A.jpg",
                "description": "Aus Google Fotos Takeout",
                "photoTakenTime": {"timestamp": "1763200000"},
                "geoDataExif": {"latitude": 53.558, "longitude": 9.808},
            }),
        )
        archive.writestr("Takeout/Google Fotos/B.jpg", img_b)
        archive.writestr(
            "Takeout/Google Fotos/B.jpg.json",
            json.dumps({
                "title": "B.jpg",
                "photoTakenTime": {"timestamp": "1763200500"},
                "geoData": {"latitude": 53.558, "longitude": 9.808},
            }),
        )

    res = client.post(
        "/api/uploads",
        files={"files[]": ("google-photos.zip", zip_buf.getvalue(), "application/zip")},
        headers={"Origin": "http://localhost:3000"},
    )
    assert res.status_code == 202
    items = res.json()["items"]
    assert len(items) == 2
    assert items[0]["observation_id"] != items[1]["observation_id"]

    detail_a = observations_route.get_observation(items[0]["observation_id"])
    assert detail_a["description"] == "Aus Google Fotos Takeout"
    assert detail_a["space_time"]["lat"] == 53.558
    assert detail_a["space_time"]["lon"] == 9.808
    assert detail_a["space_time"]["time_source"] == "google"
    assert detail_a["media"][0]["exif"].get("google_takeout") is True

    with db(config) as conn:
        gemini_jobs = conn.execute(
            "SELECT obs_id, status FROM processing_jobs WHERE kind='gemini' ORDER BY created_at_ms"
        ).fetchall()
    assert len(gemini_jobs) == 2
    assert {r["obs_id"] for r in gemini_jobs} == {items[0]["observation_id"], items[1]["observation_id"]}


def test_own_agy_bridge_resolves_models_and_forwards_images_directly(tmp_path, monkeypatch):
    import base64
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    backend_dir = str(Path(__file__).resolve().parents[1])
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    import agy_bridge

    assert agy_bridge.resolve_model("Gemini 4 Hydrogen (Medium)") == "gemini-4-hydrogen-medium"
    assert agy_bridge.resolve_model("gemini-4-hydrogen-high") == "gemini-4-hydrogen-high"

    img_b64 = base64.b64encode(_jpeg()).decode("ascii")
    prompt_text, images = agy_bridge.extract_prompt_and_images([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Bestimme den Pilz."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            ],
        }
    ])
    assert prompt_text == "Bestimme den Pilz."
    assert len(images) == 1 and images[0][0] == "jpg"

    fake_agy = tmp_path / "agy"
    fake_agy.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("AGY_BIN", str(fake_agy))

    captured = {}

    def fake_run(cmd, cwd, capture_output, text, env, timeout):
        captured["cmd"] = cmd
        captured["cwd_files"] = sorted(p.name for p in agy_bridge.Path(cwd).iterdir())
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": '{"title":"Nebelkappe"}', "usage": {"total_tokens": 42}}),
            stderr="",
        )

    monkeypatch.setattr(agy_bridge.subprocess, "run", fake_run)
    text, usage = agy_bridge.run_agy("gemini-4-hydrogen-medium", prompt_text, images)
    assert text == '{"title":"Nebelkappe"}'
    assert usage["total_tokens"] == 42
    assert "--model" in captured["cmd"] and "gemini-4-hydrogen-medium" in captured["cmd"]
    assert captured["cwd_files"] == ["image_0.jpg"]



def test_high_megapixel_photo_is_accepted_and_downscaled(config):
    from app.services.photo_ingest import ingest_image
    from app.services.upload_ingest import validate_image

    migrate(config)
    # 50.1 MP (e.g. Pixel 8 Pro 50 MP mode: 8160 x 6144) exceeds the old 40 MP cap.
    buf = io.BytesIO()
    Image.new("RGB", (8160, 6144), (40, 80, 50)).save(buf, "JPEG", quality=60)
    data = buf.getvalue()

    fmt, width, height, *_ = validate_image("pixel50mp.jpg", data, "Europe/Berlin")
    assert fmt == "JPEG" and (width, height) == (8160, 6144)

    media_id, status = ingest_image(
        source="local", external_id="pixel50mp", filename="pixel50mp.jpg", data=data, config=config
    )
    assert status == "imported" and media_id
    with Image.open(config.photo_storage_dir / media_id / "image.jpg") as stored:
        assert max(stored.size) <= 2560


def test_gps_coordinate_handles_bytes_and_nul_padded_refs():
    import pytest
    from app.services.photo_ingest import _gps_coordinate

    assert _gps_coordinate((53.0, 33.0, 0.0), b"N\x00") == pytest.approx(53.55)
    assert _gps_coordinate((9.0, 48.0, 0.0), "w\x00 ") == pytest.approx(-9.8)
    assert _gps_coordinate((53.0, 33.0, 0.0), b"\x00") is None

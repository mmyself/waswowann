from __future__ import annotations

import hashlib
import io
import json
import math
import stat
import uuid
import zipfile
from pathlib import PurePosixPath

from PIL import Image, UnidentifiedImageError

from app.config import Settings, settings
from app.db import db, now_ms
from app.services.gaussian_fusion import TIMELINE_ORDER
from app.services.photo_ingest import MAX_BYTES, MAX_PIXELS, SUPPORTED_FORMATS, IngestError, _metadata, ingest_image

MAX_REQUEST = 500 * 1024 * 1024
MAX_COUNT = 250
MAX_ZIP = 1024 * 1024 * 1024


def validate_image(filename: str, data: bytes, timezone_name: str) -> tuple[str, int, int, dict, float | None, float | None, int | None]:
    if not data:
        raise IngestError("EMPTY_FILE")
    if len(data) > MAX_BYTES:
        raise IngestError("IMAGE_TOO_LARGE")
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in {".heic", ".heif"}:
        raise IngestError("UNSUPPORTED_MEDIA_TYPE")
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = image.format
            if fmt not in SUPPORTED_FORMATS or suffix not in SUPPORTED_FORMATS[fmt]:
                raise IngestError("UNSUPPORTED_MEDIA_TYPE")
            if image.width < 1 or image.height < 1 or image.width * image.height > MAX_PIXELS:
                raise IngestError("IMAGE_DIMENSIONS_EXCEEDED")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            raw, lat, lon, observed, time_source = _metadata(image, timezone_name)
            return fmt, image.width, image.height, raw, lat, lon, observed
    except IngestError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise IngestError("INVALID_IMAGE") from exc


def _parse_google_sidecar(raw_bytes: bytes) -> dict | None:
    """Extract timestamp, geoData, and description from a Google Photos JSON sidecar."""
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    meta: dict = {}
    for key in ("photoTakenTime", "creationTime"):
        node = payload.get(key)
        if isinstance(node, dict) and node.get("timestamp") is not None:
            try:
                ts_sec = int(str(node["timestamp"]))
                if ts_sec > 0:
                    meta["observed_at_ms"] = ts_sec * 1000
                    break
            except (ValueError, TypeError):
                pass
    for key, from_exif in (("geoDataExif", True), ("geoData", False)):
        geo = payload.get(key)
        if isinstance(geo, dict):
            try:
                lat = float(geo.get("latitude"))
                lon = float(geo.get("longitude"))
                if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat != 0.0 or lon != 0.0):
                    meta["lat"] = lat
                    meta["lon"] = lon
                    meta["from_exif"] = from_exif
                    break
            except (TypeError, ValueError):
                pass
    desc = payload.get("description")
    if isinstance(desc, str) and desc.strip():
        meta["description"] = desc.strip()[:10000]
    return meta or None


def _match_sidecar(image_path: PurePosixPath, sidecars: dict[str, bytes]) -> dict | None:
    if not sidecars:
        return None
    full_lower = image_path.as_posix().lower()
    candidates = [
        f"{full_lower}.json",
        f"{full_lower}.supplemental-metadata.json",
        image_path.with_suffix(".json").as_posix().lower(),
    ]
    for cand in candidates:
        if cand in sidecars:
            return _parse_google_sidecar(sidecars[cand])
    folder = image_path.parent.as_posix().lower()
    prefix = f"{'' if folder == '.' else folder + '/'}{image_path.name.lower()}."
    for key, raw in sidecars.items():
        if key.startswith(prefix) and key.endswith(".json"):
            return _parse_google_sidecar(raw)
    return None


def unpack_uploads_with_metadata(
    files: list[tuple[str, bytes]],
) -> list[tuple[str, bytes | None, str | None, dict | None]]:
    """Expand supported images from ZIP data (including Google Takeout JSON sidecars)."""
    total = 0
    output: list[tuple[str, bytes | None, str | None, dict | None]] = []
    for filename, data in files:
        total += len(data)
        if total > MAX_REQUEST:
            raise ValueError("REQUEST_TOO_LARGE")
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix in {".heic", ".heif"}:
            output.append((PurePosixPath(filename).name, None, "UNSUPPORTED_MEDIA_TYPE", None))
            continue
        if suffix != ".zip":
            output.append((PurePosixPath(filename).name, data, None, None))
            if len(output) > MAX_COUNT:
                raise ValueError("TOO_MANY_IMAGES")
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                expanded = 0
                sidecars: dict[str, bytes] = {}
                raw_images: list[tuple[PurePosixPath, bytes | None, str | None]] = []
                for info in archive.infolist():
                    path = PurePosixPath(info.filename.replace("\\", "/"))
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ValueError("UNSAFE_ZIP")
                    if info.is_dir() or "__MACOSX" in path.parts:
                        continue
                    if path.is_absolute() or ".." in path.parts or (path.parts and path.parts[0].endswith(":")) or info.flag_bits & 1:
                        raise ValueError("UNSAFE_ZIP")
                    if path.suffix.lower() == ".zip":
                        raise ValueError("NESTED_ZIP")
                    expanded += info.file_size
                    if expanded > MAX_ZIP or info.file_size > max(1, info.compress_size) * 100:
                        raise ValueError("ZIP_LIMIT_EXCEEDED")
                    sfx = path.suffix.lower()
                    if sfx == ".json" and info.file_size <= 256 * 1024:
                        with archive.open(info) as entry:
                            sidecars[path.as_posix().lower()] = entry.read(256 * 1024 + 1)
                        continue
                    if sfx not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
                        continue
                    if info.file_size > MAX_BYTES:
                        raw_images.append((path, None, "IMAGE_TOO_LARGE"))
                        if len(output) + len(raw_images) > MAX_COUNT:
                            raise ValueError("TOO_MANY_IMAGES")
                        continue
                    with archive.open(info) as entry:
                        item = bytearray()
                        while True:
                            chunk = entry.read(min(1024 * 1024, MAX_BYTES + 1 - len(item)))
                            if not chunk:
                                break
                            item.extend(chunk)
                            if len(item) > MAX_BYTES:
                                raise ValueError("IMAGE_TOO_LARGE")
                    if len(item) != info.file_size:
                        raise ValueError("INVALID_ZIP")
                    raw_images.append((path, bytes(item), None))
                    if len(output) + len(raw_images) > MAX_COUNT:
                        raise ValueError("TOO_MANY_IMAGES")
                for path, img_bytes, err in raw_images:
                    meta = _match_sidecar(path, sidecars) if img_bytes is not None else None
                    output.append((path.name, img_bytes, err, meta))
        except zipfile.BadZipFile as exc:
            raise ValueError("INVALID_ZIP") from exc
    return output


def unpack_uploads(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes | None, str | None]]:
    """Expand supported images from ZIP data without writing or extracting files."""
    return [(name, data, err) for name, data, err, _ in unpack_uploads_with_metadata(files)]


def queue_observation_jobs(observation_id: str, config: Settings | None = None) -> None:
    """Enqueue combined multi-photo Gemini analysis job."""
    active = config or settings
    with db(active) as conn:
        conn.execute("BEGIN IMMEDIATE")
        media_rows = conn.execute(f"SELECT sha256 FROM media WHERE obs_id=? ORDER BY {TIMELINE_ORDER}", (observation_id,)).fetchall()
        if not media_rows:
            return
        combined_key = (
            media_rows[0]["sha256"]
            if len(media_rows) == 1
            else hashlib.sha256("|".join(r["sha256"] for r in media_rows).encode("utf-8")).hexdigest()
        )
        latest_note = conn.execute(
            "SELECT id, status FROM gemini_notes WHERE obs_id=? ORDER BY created_at_ms DESC, id DESC LIMIT 1",
            (observation_id,),
        ).fetchone()
        existing_job = conn.execute(
            "SELECT 1 FROM processing_jobs WHERE obs_id=? AND kind='gemini' AND status IN ('queued','running') AND input_key=?",
            (observation_id, combined_key),
        ).fetchone()
        if existing_job:
            return
        note_id = None
        stamp = now_ms()
        if latest_note and latest_note["status"] in ("not_requested", "queued"):
            note_id = latest_note["id"]
            conn.execute("UPDATE gemini_notes SET status='queued' WHERE id=?", (note_id,))
            conn.execute(
                "UPDATE processing_jobs SET status='failed', error_code='BATCH_SUPERSEDED', updated_at_ms=? "
                "WHERE obs_id=? AND kind='gemini' AND status='queued' AND note_id=?",
                (stamp, observation_id, note_id),
            )
        else:
            note_id = str(uuid.uuid4())
            parent_id = latest_note["id"] if latest_note else None
            q_text = "Die Fotoauswahl wurde aktualisiert – bitte die Einordnung anhand der Fotos prüfen." if parent_id else None
            conn.execute(
                "INSERT INTO gemini_notes(id,obs_id,source,status,question_text,parent_note_id,created_at_ms) VALUES(?,?,?,?,?,?,?)",
                (note_id, observation_id, "gemini_api", "queued", q_text, parent_id, stamp),
            )
        conn.execute(
            "INSERT INTO processing_jobs(id,obs_id,kind,note_id,input_key,status,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,'queued',?,?)",
            (str(uuid.uuid4()), observation_id, "gemini", note_id, combined_key, stamp, stamp),
        )
    from app.services.processing_jobs import runner
    runner.notify()


def import_upload(
    filename: str,
    data: bytes,
    config: Settings = settings,
    browser_location: dict | None = None,
    timezone_name: str | None = None,
    *,
    target_observation_id: str | None = None,
    defer_jobs: bool = False,
    google_metadata: dict | None = None,
) -> dict:
    name = PurePosixPath(filename).name
    digest = hashlib.sha256(data).hexdigest()
    media_id, result = ingest_image(
        source="local",
        external_id=f"upload:{digest}",
        filename=name,
        data=data,
        config=config,
        observation_id=target_observation_id,
        attach_to_existing=target_observation_id is not None,
        timezone_name=timezone_name,
        browser_location=browser_location,
        google_metadata=google_metadata,
    )
    with db(config) as conn:
        media = conn.execute("SELECT obs_id FROM media WHERE id=?", (media_id,)).fetchone()
    if not media:
        raise IngestError("IMAGE_IMPORT_FAILED")
    observation_id = media["obs_id"]
    if result == "imported" and not defer_jobs:
        queue_observation_jobs(observation_id, config)
    return {
        "filename": name, "observation_id": observation_id, "media_id": media_id,
        "result": "duplicate" if result == "skipped" else "imported", "error_code": None,
    }

from __future__ import annotations

import asyncio
import json
import math
from email.message import Message
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.gaussian_fusion import _xy
from app.services.photo_ingest import IngestError, browser_measurement
from app.services.upload_ingest import MAX_COUNT, MAX_REQUEST, import_upload, queue_observation_jobs, unpack_uploads, unpack_uploads_with_metadata, validate_image

_DEFAULT_UNPACK = unpack_uploads

router = APIRouter(prefix="/api/uploads", tags=["uploads"])
MAX_FIELD_BYTES = 16 * 1024


def _parse_multipart(content_type: str, body: bytes):
    header = Message()
    header["content-type"] = content_type
    boundary = header.get_param("boundary", header="content-type")
    if not boundary or len(boundary) > 200:
        raise ValueError("INVALID_MULTIPART")
    try:
        marker = b"--" + boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("INVALID_MULTIPART") from exc
    if not body.startswith(marker + b"\r\n") or not (body.endswith(marker + b"--") or body.endswith(marker + b"--\r\n")):
        raise ValueError("INVALID_MULTIPART")
    files, fields = [], {}
    for part in body.split(marker)[1:]:
        if part.startswith(b"--"):
            break
        if not part.startswith(b"\r\n"):
            raise ValueError("INVALID_MULTIPART")
        head, separator, payload = part[2:].partition(b"\r\n\r\n")
        if not separator or len(head) > 8192:
            raise ValueError("INVALID_MULTIPART")
        payload = payload[:-2] if payload.endswith(b"\r\n") else payload
        headers = {}
        for line in head.split(b"\r\n"):
            name, colon, value = line.partition(b":")
            if not colon:
                raise ValueError("INVALID_MULTIPART")
            headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
        disposition = Message()
        disposition["content-disposition"] = headers.get("content-disposition", "")
        name = disposition.get_param("name", header="content-disposition")
        filename = disposition.get_filename()
        if filename is not None and name in {"files", "files[]"}:
            safe_name = PurePosixPath(filename.replace("\\", "/")).name[:255] or "upload"
            files.append((safe_name, payload))
            if len(files) > MAX_COUNT:
                raise ValueError("TOO_MANY_IMAGES")
        elif name:
            if len(payload) > MAX_FIELD_BYTES:
                raise ValueError("INVALID_FIELD")
            fields[name] = payload
    if not files:
        raise ValueError("FILES_REQUIRED")
    return files, fields


@router.post("", status_code=202)
async def upload(request: Request):
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        return JSONResponse(status_code=415, content={"error": {"code": "MULTIPART_REQUIRED", "message": "Multipart-Anfrage erforderlich"}})
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_length = int(content_length)
            if parsed_length < 0:
                raise ValueError
            if parsed_length > MAX_REQUEST:
                return JSONResponse(status_code=413, content={"error": {"code": "REQUEST_TOO_LARGE", "message": "Anfrage überschreitet 500 MiB"}})
        except ValueError:
            return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CONTENT_LENGTH", "message": "Ungültige Anfragegröße"}})
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST:
            return JSONResponse(status_code=413, content={"error": {"code": "REQUEST_TOO_LARGE", "message": "Anfrage überschreitet 500 MiB"}})
        body.extend(chunk)
    try:
        files, fields = _parse_multipart(content_type, bytes(body))
        timezone_name = fields.get("timezone", b"").decode("utf-8").strip() or settings.import_timezone
        if len(timezone_name) > 100:
            raise ValueError("INVALID_TIMEZONE")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("INVALID_TIMEZONE")
        browser_location = None
        if "browser_location" in fields:
            browser_location = json.loads(fields["browser_location"].decode("utf-8"))
            if not isinstance(browser_location, dict):
                raise ValueError("INVALID_BROWSER_LOCATION")
            expected = {"lat", "lon", "accuracy_meters", "captured_at"}
            if not expected.issubset(browser_location):
                raise ValueError("INVALID_BROWSER_LOCATION")
            if not isinstance(browser_location["captured_at"], str):
                raise ValueError("INVALID_BROWSER_LOCATION")
            # Browser location values are passed as strings by multipart serializers; normalize them once.
            try:
                browser_location = {**browser_location,
                    "lat": float(browser_location["lat"]), "lon": float(browser_location["lon"]),
                    "accuracy_meters": float(browser_location["accuracy_meters"])}
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("INVALID_BROWSER_LOCATION") from exc
        prepared = (
            unpack_uploads_with_metadata(files)
            if unpack_uploads is _DEFAULT_UNPACK
            else [(name, data, err, None) for name, data, err in unpack_uploads(files)]
        )
        # Release the request and original ZIP buffers before processing expanded files.
        body.clear()
        files.clear()
        target_observation_id = fields.get("target_observation_id", b"").decode("utf-8").strip() or None
        group_flag = fields.get("group_same_location", b"false").decode("utf-8").strip().lower()
        group_same_location = group_flag in {"1", "true", "yes", "on"}
        use_batch_grouping = target_observation_id is not None or (group_same_location and len(prepared) > 1)
        items = []
        touched_observations: list[str] = []
        batch_anchors: list[tuple[str, float | None, float | None]] = []

        browser_fix = browser_measurement(browser_location)

        def _find_matching_anchor(filename: str, data: bytes, google_meta: dict | None) -> tuple[str | None, float | None, float | None]:
            _, _, _, _, cand_lat, cand_lon, _ = validate_image(filename, data, timezone_name)
            if cand_lat is None and google_meta:
                cand_lat, cand_lon = google_meta.get("lat"), google_meta.get("lon")
            if cand_lat is None and browser_fix:
                cand_lat, cand_lon, _ = browser_fix
            for obs_id, anchor_lat, anchor_lon in batch_anchors:
                if cand_lat is not None and cand_lon is not None and anchor_lat is not None and anchor_lon is not None:
                    east, north = _xy(cand_lat, cand_lon, anchor_lat, anchor_lon)
                    if math.hypot(east, north) <= 200.0:
                        return obs_id, cand_lat, cand_lon
                elif cand_lat is None and anchor_lat is None:
                    return obs_id, None, None
            return None, cand_lat, cand_lon

        for item_index, (filename, data, error, google_meta) in enumerate(prepared):
            if error or data is None:
                items.append({"filename": filename, "observation_id": None, "media_id": None, "result": "error", "error_code": error or "INVALID_IMAGE"})
                continue
            try:
                if not use_batch_grouping:
                    if google_meta is not None:
                        result = await asyncio.to_thread(
                            import_upload,
                            filename,
                            data,
                            settings,
                            browser_location,
                            timezone_name,
                            google_metadata=google_meta,
                        )
                    else:
                        result = await asyncio.to_thread(import_upload, filename, data, settings, browser_location, timezone_name)
                else:
                    matched_id = target_observation_id
                    cand_lat = cand_lon = None
                    if matched_id is None:
                        matched_id, cand_lat, cand_lon = await asyncio.to_thread(
                            _find_matching_anchor, filename, data, google_meta,
                        )
                    result = await asyncio.to_thread(
                        import_upload,
                        filename,
                        data,
                        settings,
                        browser_location,
                        timezone_name,
                        target_observation_id=matched_id,
                        defer_jobs=True,
                        google_metadata=google_meta,
                    )
                    obs_id = result.get("observation_id")
                    if obs_id and result.get("result") == "imported":
                        if obs_id not in touched_observations:
                            touched_observations.append(obs_id)
                        if target_observation_id is None and matched_id is None and not any(a[0] == obs_id for a in batch_anchors):
                            batch_anchors.append((obs_id, cand_lat, cand_lon))
                items.append(result)
            except IngestError as exc:
                items.append({"filename": filename, "observation_id": None, "media_id": None, "result": "error", "error_code": str(exc)[:80]})
            except (OSError, ValueError):
                items.append({"filename": filename, "observation_id": None, "media_id": None, "result": "error", "error_code": "IMAGE_IMPORT_FAILED"})
            finally:
                prepared[item_index] = (filename, None, error, None)
                data = None
        prepared.clear()
        for obs_id in touched_observations:
            await asyncio.to_thread(queue_observation_jobs, obs_id, settings)
        return JSONResponse(status_code=202, content={"items": items})
    except ValueError as exc:
        code = str(exc)
        if isinstance(exc, json.JSONDecodeError):
            code = "INVALID_BROWSER_LOCATION"
        status = 413 if code in {"REQUEST_TOO_LARGE", "TOO_MANY_IMAGES", "ZIP_LIMIT_EXCEEDED"} else 422
        return JSONResponse(status_code=status, content={"error": {"code": code[:80], "message": "Upload konnte nicht verarbeitet werden"}})
    except UnicodeDecodeError:
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_MULTIPART_FIELD", "message": "Uploadfelder sind ungültig"}})

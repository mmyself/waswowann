from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings, settings
from app.db import db, now_ms
from app.services.gaussian_fusion import recompute_observation_kalman

MAX_BYTES = 60 * 1024 * 1024
MAX_PIXELS = 250_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
SUPPORTED_FORMATS = {'JPEG': {'.jpg', '.jpeg'}, 'PNG': {'.png'}, 'WEBP': {'.webp'}}
SUPPORTED = set().union(*SUPPORTED_FORMATS.values())
MAX_PREVIEW_BYTES = 800 * 1024
MAX_THUMB_BYTES = 40 * 1024


class IngestError(ValueError):
    pass


def _rational(value):
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        try:
            return float(value.numerator) / float(value.denominator)
        except Exception:
            return None


def _gps_coordinate(values, ref):
    if not values or not ref or len(values) != 3:
        return None
    if isinstance(ref, bytes):
        ref = ref.decode('ascii', 'ignore')
    ref = str(ref).strip().strip('\x00').upper()
    parts = [_rational(x) for x in values]
    if any(x is None for x in parts):
        return None
    result = parts[0] + parts[1] / 60 + parts[2] / 3600
    if ref in ('S', 'W'):
        result = -result
    elif ref not in ('N', 'E'):
        return None
    return result


def _exif_uncertainties(
    image: Image.Image,
    raw_exif: dict,
    lat: float | None,
    observed: int | None,
    config: Settings,
) -> tuple[float | None, float | None]:
    """Derive initial Gaussian sigmas from EXIF precision tags or configured defaults."""
    exif_obj = image.getexif()
    try:
        gps = exif_obj.get_ifd(0x8825)
    except Exception:
        gps = {}
    try:
        subifd = exif_obj.get_ifd(0x8769)
    except Exception:
        subifd = {}

    sigma_meters: float | None = None
    if lat is not None:
        h_err = _rational(gps.get(31))  # GPSHPositioningError (meters)
        dop = _rational(gps.get(11))    # GPSDOP
        if h_err is not None and math.isfinite(h_err) and h_err > 0:
            sigma_meters = max(0.5, min(50_000.0, float(h_err)))
            raw_exif['gps_h_positioning_error_m'] = round(sigma_meters, 2)
            raw_exif['sigma_meters_source'] = 'exif'
        elif dop is not None and math.isfinite(dop) and dop > 0:
            sigma_meters = max(3.0, min(500.0, float(dop) * 5.0))
            raw_exif['gps_dop'] = round(float(dop), 2)
            raw_exif['sigma_meters_source'] = 'exif_dop'
        else:
            sigma_meters = float(config.fusion_default_sigma_meters)
            raw_exif['sigma_meters_source'] = 'default'

    sigma_seconds: float | None = None
    if observed is not None:
        has_offset = any(
            k in raw_exif for k in ('offset_time', 'offset_time_original', 'offset_time_digitized')
        )
        subsec = subifd.get(37521) or subifd.get(37520) or exif_obj.get(37520)
        if has_offset:
            sigma_seconds = 2.0 if subsec is not None else 5.0
            raw_exif['sigma_seconds_source'] = 'exif'
        else:
            sigma_seconds = float(config.fusion_default_sigma_seconds)
            raw_exif['sigma_seconds_source'] = 'default'

    return sigma_meters, sigma_seconds


def _metadata(image: Image.Image, timezone_name: str) -> tuple[dict, float | None, float | None, int | None, str]:
    exif_obj = image.getexif()
    raw = {}
    try:
        gps = exif_obj.get_ifd(0x8825)
    except Exception:
        gps = {}
    lat = _gps_coordinate(gps.get(2), gps.get(1))
    lon = _gps_coordinate(gps.get(4), gps.get(3))
    if lat is not None and not -90 <= lat <= 90: lat = None
    if lon is not None and not -180 <= lon <= 180: lon = None
    if lat is None or lon is None: lat = lon = None

    try:
        subifd = exif_obj.get_ifd(0x8769)
    except Exception:
        subifd = {}
    for tag, label in (
        (271, 'camera_make'), (272, 'camera_model'), (306, 'date_time'),
        (36867, 'date_time_original'), (36868, 'date_time_digitized'),
        (36880, 'offset_time'), (36881, 'offset_time_original'), (36882, 'offset_time_digitized'),
    ):
        value = subifd.get(tag) or exif_obj.get(tag)
        if isinstance(value, bytes):
            value = value.decode('utf-8', 'replace')
        if value is not None:
            raw[label] = str(value).strip('\x00 ')[:256]
    observed, source = _exif_timestamp(raw, timezone_name)
    return raw, lat, lon, observed, source


def _exif_timestamp(raw: dict, timezone_name: str) -> tuple[int | None, str]:
    for date_key, offset_key in (
        ('date_time_original', 'offset_time_original'),
        ('date_time_digitized', 'offset_time_digitized'),
        ('date_time', 'offset_time'),
    ):
        text = raw.get(date_key)
        if not isinstance(text, str):
            continue
        try:
            naive = datetime.strptime(text, '%Y:%m:%d %H:%M:%S')
        except ValueError:
            continue
        offset = raw.get(offset_key) or raw.get('offset_time')
        if date_key == 'date_time' and not offset:
            offset = raw.get('offset_time_original') or raw.get('offset_time_digitized')
        if isinstance(offset, str) and re.fullmatch(r'[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]', offset):
            hours, minutes = map(int, offset[1:].split(':'))
            delta = timedelta(hours=hours, minutes=minutes)
            aware = naive.replace(tzinfo=timezone(-delta if offset[0] == '-' else delta))
            return int(aware.timestamp()) * 1000, 'exif'
        try:
            zone = ZoneInfo(timezone_name)
            aware = naive.replace(tzinfo=zone, fold=0)
            observed = int(aware.timestamp()) * 1000
            if datetime.fromtimestamp(observed / 1000, zone).replace(tzinfo=None) != naive:
                raw['time_assumption'] = f'Nonexistent local time in {timezone_name}; time left unknown'
            elif aware.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset():
                raw['time_assumption'] = f'Ambiguous local time in {timezone_name}; time left unknown'
            else:
                raw['time_assumption'] = f'No EXIF offset; interpreted in {timezone_name}'
                return observed, 'exif'
        except (ZoneInfoNotFoundError, ValueError, OverflowError):
            raw['time_assumption'] = f'Invalid or nonexistent local time in {timezone_name}; time left unknown'
    return None, 'unknown'


def _save_bounded_jpeg(image: Image.Image, path: Path, max_bytes: int, qualities: tuple[int, ...]) -> tuple[int, int]:
    while True:
        for quality in qualities:
            output = io.BytesIO()
            image.save(output, 'JPEG', quality=quality, optimize=True)
            if output.tell() <= max_bytes:
                path.write_bytes(output.getbuffer())
                return image.size
        if max(image.size) <= 64:
            raise IngestError('PREVIEW_SIZE_LIMIT')
        image = image.resize(tuple(max(1, int(side * .8)) for side in image.size), Image.Resampling.LANCZOS)


def _find_existing(conn, source: str, external_id: str, digest: str):
    row = conn.execute('SELECT id FROM media WHERE source=? AND external_id=?', (source, external_id)).fetchone()
    if row is None and source == 'local':
        row = conn.execute("SELECT id FROM media WHERE source='local' AND sha256=?", (digest,)).fetchone()
    return row


def browser_measurement(browser_location: dict | None) -> tuple[float, float, float] | None:
    if browser_location:
        try:
            lat, lon = float(browser_location['lat']), float(browser_location['lon'])
            accuracy = float(browser_location['accuracy_meters'])
            if -90 <= lat <= 90 and -180 <= lon <= 180 and 0 < accuracy <= 100_000:
                return lat, lon, accuracy / math.sqrt(-2 * math.log(.05))
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    return None


def ingest_image(*, source: str, external_id: str, filename: str, data: bytes, config: Settings = settings,
                 google_create_time: int | None = None, observation_id: str | None = None, media_id_override: str | None = None,
                 attach_to_existing: bool = False, timezone_name: str | None = None,
                 browser_location: dict | None = None, google_metadata: dict | None = None) -> tuple[str, str]:
    if source not in {'demo', 'local', 'google_photos'}:
        raise IngestError('Unsupported image source')
    if not data:
        raise IngestError('EMPTY_FILE')
    if len(data) > MAX_BYTES:
        raise IngestError('IMAGE_TOO_LARGE')
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise IngestError('UNSUPPORTED_MEDIA_TYPE')
    media_id, obs_id = media_id_override or str(uuid.uuid4()), observation_id or str(uuid.uuid4())
    digest = hashlib.sha256(data).hexdigest()
    with db(config) as conn:
        if attach_to_existing and not conn.execute('SELECT 1 FROM observations WHERE id=?', (obs_id,)).fetchone():
            raise IngestError('OBSERVATION_NOT_FOUND')
        existing = _find_existing(conn, source, external_id, digest)
        if existing:
            return existing['id'], 'skipped'
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if suffix not in SUPPORTED_FORMATS.get(opened.format, ()):
                raise IngestError('UNSUPPORTED_MEDIA_TYPE')
            if opened.width * opened.height > MAX_PIXELS:
                raise IngestError('IMAGE_DIMENSIONS_EXCEEDED')
            opened.load()
            raw_exif, lat, lon, observed, time_source = _metadata(opened, timezone_name or config.import_timezone)
            sigma_meters, sigma_seconds = _exif_uncertainties(opened, raw_exif, lat, observed, config)
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            image = image.convert('RGBA')
            display = Image.new('RGB', image.size, (245, 245, 240))
            display.paste(image, mask=image.getchannel('A'))
    except IngestError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise IngestError('INVALID_IMAGE') from exc

    description = ''
    location_source = 'exif' if lat is not None else 'unknown'
    if google_metadata:
        raw_exif['google_takeout'] = True
        description = google_metadata.get('description', '')
        if google_create_time is None:
            google_create_time = google_metadata.get('observed_at_ms')
        if lat is None and google_metadata.get('lat') is not None and google_metadata.get('lon') is not None:
            lat, lon = google_metadata['lat'], google_metadata['lon']
            sigma_meters = config.fusion_default_sigma_meters
            location_source = 'exif' if google_metadata.get('from_exif') else 'manual'
            raw_exif['sigma_meters_source'] = 'default'
    if observed is None and google_create_time is not None:
        observed, sigma_seconds, time_source = google_create_time, config.fusion_default_sigma_seconds, 'google'
        raw_exif['sigma_seconds_source'] = 'default'
    browser = browser_measurement(browser_location) if lat is None else None
    if browser is not None:
        lat, lon, sigma_meters = browser
        location_source = 'browser'
        raw_exif['sigma_meters_source'] = 'browser'
        if isinstance(browser_location.get('captured_at'), str):
            raw_exif['browser_location_captured_at'] = browser_location['captured_at']

    config.photo_storage_dir.mkdir(parents=True, exist_ok=True)
    config.temp_storage_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='ingest-', dir=config.temp_storage_dir))
    final_dir = config.photo_storage_dir / media_id
    published = False
    try:
        width, height = _save_bounded_jpeg(display, stage / 'image.jpg', MAX_PREVIEW_BYTES, (86, 74, 62, 50, 38))
        display.thumbnail((480, 480), Image.Resampling.LANCZOS)
        _save_bounded_jpeg(display, stage / 'thumbnail.jpg', MAX_THUMB_BYTES, (78, 65, 52, 39))
        with db(config) as conn:
            conn.execute('BEGIN IMMEDIATE')
            obs_exists = conn.execute('SELECT 1 FROM observations WHERE id=?', (obs_id,)).fetchone()
            if attach_to_existing and not obs_exists:
                raise IngestError('OBSERVATION_NOT_FOUND')
            existing = _find_existing(conn, source, external_id, digest)
            if existing:
                return existing['id'], 'skipped'
            created = now_ms()
            name = Path(filename).name[:255]
            if not obs_exists:
                conn.execute('INSERT INTO observations(id,title,description,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?)',
                             (obs_id, name or 'Foto', description, created, created))
                conn.execute('INSERT INTO gemini_notes(id,obs_id,source,status,created_at_ms) VALUES(?,?,?,?,?)',
                             (str(uuid.uuid4()), obs_id, 'placeholder', 'not_requested', created))
            else:
                conn.execute('UPDATE observations SET updated_at_ms=? WHERE id=?', (created, obs_id))
            conn.execute('''INSERT INTO media(
                id,obs_id,source,external_id,original_name,local_path,thumbnail_path,mime_type,width,height,sha256,exif_json,created_at_ms,
                observed_at_ms,sigma_seconds,time_source,lat,lon,sigma_meters,location_source
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (media_id, obs_id, source, external_id, name, f'{media_id}/image.jpg', f'{media_id}/thumbnail.jpg', 'image/jpeg',
                 width, height, digest, json.dumps(raw_exif, ensure_ascii=False), created,
                 observed, sigma_seconds, time_source, lat, lon, sigma_meters, location_source))
            recompute_observation_kalman(conn, obs_id, config=config)
            os.replace(stage, final_dir)
            published = True
    except Exception:
        if published:
            shutil.rmtree(final_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return media_id, 'imported'

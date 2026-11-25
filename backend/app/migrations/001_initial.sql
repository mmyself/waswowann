PRAGMA foreign_keys = ON;
CREATE TABLE observations (
 id TEXT PRIMARY KEY,
 category TEXT NOT NULL DEFAULT 'Unbekannt' CHECK(category IN ('Pilz','Pflanze','Person','Ding','Ereignis','Unbekannt')),
 title TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '',
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL
);
CREATE TABLE space_time_gauss (
 obs_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
 lat REAL CHECK(lat IS NULL OR lat BETWEEN -90 AND 90),
 lon REAL CHECK(lon IS NULL OR lon BETWEEN -180 AND 180),
 sigma_meters REAL CHECK(sigma_meters IS NULL OR sigma_meters > 0),
 observed_at_ms INTEGER,
 sigma_seconds REAL CHECK(sigma_seconds IS NULL OR sigma_seconds > 0),
 location_source TEXT NOT NULL DEFAULT 'unknown' CHECK(location_source IN ('unknown','exif','manual','demo')),
 time_source TEXT NOT NULL DEFAULT 'unknown' CHECK(time_source IN ('unknown','exif','google','manual','demo')),
 fused_group_id TEXT,
 CHECK((lat IS NULL AND lon IS NULL) OR (lat IS NOT NULL AND lon IS NOT NULL)),
 CHECK(sigma_meters IS NULL OR lat IS NOT NULL),
 CHECK(sigma_seconds IS NULL OR observed_at_ms IS NOT NULL)
);
CREATE TABLE media (
 id TEXT PRIMARY KEY,
 obs_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
 source TEXT NOT NULL CHECK(source IN ('demo','local','google_photos')),
 external_id TEXT NOT NULL,
 original_name TEXT NOT NULL,
 local_path TEXT NOT NULL,
 thumbnail_path TEXT NOT NULL,
 mime_type TEXT NOT NULL,
 width INTEGER NOT NULL CHECK(width > 0), height INTEGER NOT NULL CHECK(height > 0),
 sha256 TEXT NOT NULL, exif_json TEXT NOT NULL DEFAULT '{}', created_at_ms INTEGER NOT NULL,
 UNIQUE(source,external_id)
);
CREATE INDEX idx_media_obs ON media(obs_id);
CREATE TABLE gemini_notes (
 id TEXT PRIMARY KEY, obs_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
 source TEXT NOT NULL CHECK(source IN ('placeholder','takeout_import','gemini_api')),
 status TEXT NOT NULL CHECK(status IN ('not_requested','ready','error')),
 raw_text TEXT NOT NULL DEFAULT '', structured_json TEXT NOT NULL DEFAULT '{}', created_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_gemini_obs ON gemini_notes(obs_id);
CREATE TABLE weather_slices (
 obs_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'not_requested' CHECK(status IN ('not_requested','ready','error')),
 anchor_at_ms INTEGER, slice_24h_json TEXT NOT NULL DEFAULT '[]', slice_7d_json TEXT NOT NULL DEFAULT '[]',
 slice_12w_json TEXT NOT NULL DEFAULT '[]', fetched_at_ms INTEGER
);
CREATE TABLE picker_sessions (
 id TEXT PRIMARY KEY, google_session_id TEXT NOT NULL UNIQUE, owner_session_hash TEXT NOT NULL,
 picker_uri TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('waiting','ready','importing','completed','cancelled','expired','failed')),
 poll_interval_ms INTEGER NOT NULL, polling_deadline_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
 next_poll_at_ms INTEGER, last_error_code TEXT, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
);
CREATE TABLE import_jobs (
 id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('demo','local','google_photos')),
 picker_session_id TEXT REFERENCES picker_sessions(id), status TEXT NOT NULL CHECK(status IN ('queued','running','completed','partial','failed')),
 total INTEGER, processed INTEGER NOT NULL DEFAULT 0, imported INTEGER NOT NULL DEFAULT 0,
 skipped INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, errors_json TEXT NOT NULL DEFAULT '[]',
 created_at_ms INTEGER NOT NULL, finished_at_ms INTEGER
);
CREATE UNIQUE INDEX idx_one_job_per_picker_session ON import_jobs(picker_session_id) WHERE picker_session_id IS NOT NULL;
CREATE INDEX idx_space_time_observed ON space_time_gauss(observed_at_ms);
CREATE INDEX idx_space_time_location ON space_time_gauss(lat,lon);

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

CREATE TABLE gemini_notes_new (
 id TEXT PRIMARY KEY, obs_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
 source TEXT NOT NULL CHECK(source IN ('placeholder','takeout_import','gemini_api','antigravity')),
 status TEXT NOT NULL CHECK(status IN ('not_requested','queued','running','ready','error')),
 raw_text TEXT NOT NULL DEFAULT '', structured_json TEXT NOT NULL DEFAULT '{}', created_at_ms INTEGER NOT NULL,
 parent_note_id TEXT REFERENCES gemini_notes_new(id) ON DELETE SET NULL,
 question_text TEXT, provider TEXT, model TEXT, error_code TEXT
);
INSERT INTO gemini_notes_new(id,obs_id,source,status,raw_text,structured_json,created_at_ms)
 SELECT id,obs_id,source,CASE status WHEN 'ready' THEN 'ready' WHEN 'error' THEN 'error' ELSE 'not_requested' END,raw_text,structured_json,created_at_ms FROM gemini_notes;
DROP TABLE gemini_notes;
ALTER TABLE gemini_notes_new RENAME TO gemini_notes;
CREATE INDEX idx_gemini_obs ON gemini_notes(obs_id);
CREATE INDEX idx_gemini_parent ON gemini_notes(parent_note_id);

CREATE TABLE weather_slices_new (
 obs_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'not_requested' CHECK(status IN ('not_requested','queued','running','ready','partial','error','unavailable')),
 anchor_at_ms INTEGER, slice_24h_json TEXT NOT NULL DEFAULT '[]', slice_7d_json TEXT NOT NULL DEFAULT '[]',
 slice_12w_json TEXT NOT NULL DEFAULT '[]', fetched_at_ms INTEGER, cache_key TEXT, error_code TEXT,
 coverage_json TEXT NOT NULL DEFAULT '{}', sources_json TEXT NOT NULL DEFAULT '[]'
);
INSERT INTO weather_slices_new(obs_id,status,anchor_at_ms,slice_24h_json,slice_7d_json,slice_12w_json,fetched_at_ms)
 SELECT obs_id,status,anchor_at_ms,slice_24h_json,slice_7d_json,slice_12w_json,fetched_at_ms FROM weather_slices;
DROP TABLE weather_slices;
ALTER TABLE weather_slices_new RENAME TO weather_slices;

CREATE TABLE weather_cache (
 cache_key TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, anchor_at_ms INTEGER NOT NULL,
 schema_version INTEGER NOT NULL, slice_24h_json TEXT NOT NULL DEFAULT '[]', slice_7d_json TEXT NOT NULL DEFAULT '[]',
 slice_12w_json TEXT NOT NULL DEFAULT '[]', coverage_json TEXT NOT NULL DEFAULT '{}', sources_json TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL CHECK(status IN ('ready','partial','error','unavailable')), fetched_at_ms INTEGER NOT NULL,
 expires_at_ms INTEGER NOT NULL, error_code TEXT
);
CREATE TABLE processing_jobs (
 id TEXT PRIMARY KEY, obs_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('gemini','weather')), note_id TEXT REFERENCES gemini_notes(id) ON DELETE CASCADE,
 input_key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at_ms INTEGER NOT NULL DEFAULT 0, error_code TEXT,
 created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_active_processing_job ON processing_jobs(kind,obs_id,input_key) WHERE status IN ('queued','running');
CREATE INDEX idx_processing_due ON processing_jobs(status,next_attempt_at_ms);
CREATE TABLE fusion_groups (
 id TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, sigma_meters REAL NOT NULL CHECK(sigma_meters>0),
 observed_at_ms INTEGER NOT NULL, sigma_seconds REAL NOT NULL CHECK(sigma_seconds>0), updated_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_space_time_fused_group ON space_time_gauss(fused_group_id);

CREATE TABLE space_time_gauss_new (
 obs_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
 lat REAL CHECK(lat IS NULL OR lat BETWEEN -90 AND 90), lon REAL CHECK(lon IS NULL OR lon BETWEEN -180 AND 180),
 sigma_meters REAL CHECK(sigma_meters IS NULL OR sigma_meters>0), observed_at_ms INTEGER,
 sigma_seconds REAL CHECK(sigma_seconds IS NULL OR sigma_seconds>0),
 location_source TEXT NOT NULL DEFAULT 'unknown' CHECK(location_source IN ('unknown','exif','manual','demo','browser')),
 time_source TEXT NOT NULL DEFAULT 'unknown' CHECK(time_source IN ('unknown','exif','google','manual','demo','browser')),
 fused_group_id TEXT,
 CHECK((lat IS NULL AND lon IS NULL) OR (lat IS NOT NULL AND lon IS NOT NULL)),
 CHECK(sigma_meters IS NULL OR lat IS NOT NULL), CHECK(sigma_seconds IS NULL OR observed_at_ms IS NOT NULL)
);
INSERT INTO space_time_gauss_new SELECT obs_id,lat,lon,sigma_meters,observed_at_ms,sigma_seconds,location_source,time_source,fused_group_id FROM space_time_gauss;
DROP TABLE space_time_gauss;
ALTER TABLE space_time_gauss_new RENAME TO space_time_gauss;
CREATE INDEX idx_space_time_observed ON space_time_gauss(observed_at_ms);
CREATE INDEX idx_space_time_location ON space_time_gauss(lat,lon);
CREATE INDEX idx_space_time_fused_group ON space_time_gauss(fused_group_id);

PRAGMA foreign_key_check;
COMMIT;
PRAGMA foreign_keys=ON;

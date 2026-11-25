ALTER TABLE media ADD COLUMN observed_at_ms INTEGER;
ALTER TABLE media ADD COLUMN sigma_seconds REAL CHECK(sigma_seconds IS NULL OR (sigma_seconds > 0 AND observed_at_ms IS NOT NULL));
ALTER TABLE media ADD COLUMN time_source TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE media ADD COLUMN lat REAL CHECK(lat IS NULL OR lat BETWEEN -90 AND 90);
ALTER TABLE media ADD COLUMN lon REAL CHECK((lat IS NULL AND lon IS NULL) OR (lat IS NOT NULL AND lon IS NOT NULL AND lon BETWEEN -180 AND 180));
ALTER TABLE media ADD COLUMN sigma_meters REAL CHECK(sigma_meters IS NULL OR (sigma_meters > 0 AND lat IS NOT NULL));
ALTER TABLE media ADD COLUMN location_source TEXT NOT NULL DEFAULT 'unknown';

UPDATE media SET (lat, lon, sigma_meters, location_source, observed_at_ms, sigma_seconds, time_source) = (
    SELECT s.lat, s.lon, s.sigma_meters, COALESCE(s.location_source, 'unknown'), s.observed_at_ms, s.sigma_seconds, COALESCE(s.time_source, 'unknown')
    FROM observations AS o LEFT JOIN space_time_gauss AS s ON s.obs_id=o.id WHERE o.id=media.obs_id
);

CREATE TEMP TABLE legacy_group_members AS
SELECT s.fused_group_id AS group_id, o.id
FROM fusion_groups AS g
JOIN space_time_gauss AS s ON s.fused_group_id = g.id
JOIN observations AS o ON s.obs_id = o.id
ORDER BY group_id, o.created_at_ms, o.id;

DROP INDEX idx_media_obs;
CREATE INDEX idx_media_timeline ON media(obs_id, observed_at_ms IS NULL, observed_at_ms, created_at_ms, id);
DROP TABLE space_time_gauss;
DROP TABLE fusion_groups;

CREATE TABLE space_time_gauss (
    obs_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
    lat REAL CHECK(lat IS NULL OR lat BETWEEN -90 AND 90),
    lon REAL CHECK(lon IS NULL OR lon BETWEEN -180 AND 180),
    sigma_meters REAL CHECK(sigma_meters IS NULL OR sigma_meters > 0),
    observed_at_ms INTEGER,
    sigma_seconds REAL CHECK(sigma_seconds IS NULL OR sigma_seconds > 0),
    location_source TEXT NOT NULL DEFAULT 'unknown',
    time_source TEXT NOT NULL DEFAULT 'unknown',
    first_observed_at_ms INTEGER,
    last_observed_at_ms INTEGER,
    location_measurement_count INTEGER NOT NULL DEFAULT 0 CHECK(location_measurement_count >= 0),
    time_of_day TEXT NOT NULL DEFAULT 'null',
    season TEXT NOT NULL DEFAULT 'null',
    CHECK((lat IS NULL AND lon IS NULL) OR (lat IS NOT NULL AND lon IS NOT NULL)),
    CHECK(sigma_meters IS NULL OR lat IS NOT NULL),
    CHECK(sigma_seconds IS NULL OR observed_at_ms IS NOT NULL)
);
CREATE INDEX idx_space_time_observed ON space_time_gauss(observed_at_ms);
CREATE INDEX idx_space_time_location ON space_time_gauss(lat, lon);

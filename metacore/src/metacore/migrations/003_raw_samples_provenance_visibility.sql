-- Add raw high-frequency time-series buffer, provenance links, epistemic confidence, and visibility scope
ALTER TABLE entities ADD COLUMN visibility TEXT NOT NULL DEFAULT 'normal' CHECK(visibility IN ('normal', 'local_only'));
ALTER TABLE states ADD COLUMN evidence_refs_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE states ADD COLUMN assertion_confidence REAL NOT NULL DEFAULT 1.0 CHECK(assertion_confidence > 0.0 AND assertion_confidence <= 1.0);
ALTER TABLE states ADD COLUMN visibility TEXT NOT NULL DEFAULT 'normal' CHECK(visibility IN ('normal', 'local_only'));

CREATE TABLE raw_samples (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    property_name TEXT NOT NULL CHECK(length(trim(property_name)) > 0),
    time_ms INTEGER NOT NULL,
    value_num REAL NOT NULL,
    value_sigma REAL CHECK(value_sigma IS NULL OR value_sigma >= 0.0),
    value_unit TEXT NOT NULL DEFAULT '',
    measurement_source TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    evidence_ref TEXT NOT NULL DEFAULT '',
    rolled_up_state_id TEXT REFERENCES states(id) ON DELETE SET NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX idx_raw_samples_entity_prop_time ON raw_samples(entity_id, property_name, time_ms);
CREATE INDEX idx_raw_samples_unrolled ON raw_samples(rolled_up_state_id, entity_id, property_name, time_ms);

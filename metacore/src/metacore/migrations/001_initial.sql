-- WasWoWann MetaCore initial schema (v2 principles)
-- 4 core tables: entities, states, transformations, edges

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    kind TEXT NOT NULL CHECK(kind IN ('worldline', 'type')),
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'observed' CHECK(source IN ('observed', 'derived')),
    parent_entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE states (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    modality TEXT NOT NULL CHECK(modality IN ('observed', 'predicted', 'desired', 'cancelled')),
    source TEXT NOT NULL CHECK(source IN ('measured', 'ai_prior', 'derived', 'user_goal')),
    measurement_source TEXT NOT NULL DEFAULT '',
    property_name TEXT NOT NULL CHECK(length(trim(property_name)) > 0),
    label TEXT NOT NULL CHECK(length(trim(label)) > 0),

    -- Value + uncertainty (for observed / predicted; targets carry bounds below instead of sigma)
    value_num REAL,
    value_sigma REAL CHECK(value_sigma IS NULL OR value_sigma >= 0),
    value_unit TEXT NOT NULL DEFAULT '',
    value_text TEXT,
    distribution_kind TEXT NOT NULL DEFAULT 'normal' CHECK(distribution_kind IN ('normal', 'discrete', 'point')),

    -- Space ("Wo")
    lat REAL CHECK(lat IS NULL OR lat BETWEEN -90 AND 90),
    lon REAL CHECK(lon IS NULL OR lon BETWEEN -180 AND 180),
    sigma_meters REAL CHECK(sigma_meters IS NULL OR sigma_meters > 0),
    location_label TEXT NOT NULL DEFAULT '',

    -- Time ("Wann")
    time_ms INTEGER,
    sigma_seconds REAL CHECK(sigma_seconds IS NULL OR sigma_seconds > 0),

    -- Target bounds ("desired", strictly separated from uncertainty sigma)
    target_min REAL,
    target_max REAL,
    target_preferred REAL,
    target_value_text TEXT,
    target_earliest_ms INTEGER,
    target_latest_ms INTEGER,
    target_radius_meters REAL CHECK(target_radius_meters IS NULL OR target_radius_meters > 0),
    threshold_ok REAL NOT NULL DEFAULT 0.8 CHECK(threshold_ok >= 0.0 AND threshold_ok <= 1.0),
    threshold_fail REAL NOT NULL DEFAULT 0.2 CHECK(threshold_fail >= 0.0 AND threshold_fail <= 1.0 AND threshold_fail <= threshold_ok),
    priority REAL NOT NULL DEFAULT 1.0 CHECK(priority >= 0.0),

    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,

    CHECK((lat IS NULL AND lon IS NULL) OR (lat IS NOT NULL AND lon IS NOT NULL)),
    CHECK(value_sigma IS NULL OR value_num IS NOT NULL),
    CHECK(sigma_meters IS NULL OR lat IS NOT NULL),
    CHECK(sigma_seconds IS NULL OR time_ms IS NOT NULL),
    CHECK(modality != 'desired' OR (value_sigma IS NULL AND sigma_meters IS NULL AND sigma_seconds IS NULL)),
    CHECK(target_min IS NULL OR target_max IS NULL OR target_min <= target_max),
    CHECK(target_earliest_ms IS NULL OR target_latest_ms IS NULL OR target_earliest_ms <= target_latest_ms)
);

CREATE TABLE transformations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    modality TEXT NOT NULL CHECK(modality IN ('happened', 'planned', 'template', 'cancelled')),
    driver TEXT NOT NULL CHECK(driver IN ('actor', 'nature')),
    template_id TEXT REFERENCES transformations(id) ON DELETE SET NULL,
    parent_id TEXT REFERENCES transformations(id) ON DELETE CASCADE,
    duration_seconds REAL CHECK(duration_seconds IS NULL OR duration_seconds >= 0),
    duration_sigma_seconds REAL CHECK(duration_sigma_seconds IS NULL OR duration_sigma_seconds > 0),
    duration_source TEXT NOT NULL DEFAULT 'ai_prior' CHECK(duration_source IN ('measured', 'ai_prior', 'derived')),
    cost REAL NOT NULL DEFAULT 0.0 CHECK(cost >= 0.0),
    success_prob REAL NOT NULL DEFAULT 1.0 CHECK(success_prob > 0.0 AND success_prob <= 1.0),
    formula_json TEXT NOT NULL DEFAULT '{}',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,

    CHECK(duration_sigma_seconds IS NULL OR duration_seconds IS NOT NULL)
);

CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    transformation_id TEXT NOT NULL REFERENCES transformations(id) ON DELETE CASCADE,
    state_id TEXT NOT NULL REFERENCES states(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
    role TEXT NOT NULL CHECK(role IN ('consumes', 'uses', 'triggers', 'produces')),
    logic_group TEXT NOT NULL DEFAULT 'AND' CHECK(logic_group IN ('AND', 'CHOICE_OR', 'WORLD_OR')),
    branch_key TEXT NOT NULL DEFAULT 'default',
    is_selected INTEGER NOT NULL DEFAULT 1 CHECK(is_selected IN (0, 1)),
    outcome TEXT NOT NULL DEFAULT 'pending' CHECK(outcome IN ('pending', 'chosen', 'not_chosen', 'occurred', 'did_not_occur')),
    probability REAL CHECK(probability IS NULL OR (probability > 0.0 AND probability <= 1.0)),
    ratio_mean REAL,
    ratio_sigma REAL CHECK(ratio_sigma IS NULL OR ratio_sigma >= 0),
    resource_released INTEGER NOT NULL DEFAULT 0 CHECK(resource_released IN (0, 1)),
    created_at_ms INTEGER NOT NULL,

    CHECK(ratio_sigma IS NULL OR ratio_mean IS NOT NULL)
);

CREATE INDEX idx_entities_parent ON entities(parent_entity_id);
CREATE INDEX idx_states_entity_prop_time ON states(entity_id, property_name, modality, time_ms);
CREATE INDEX idx_states_modality ON states(modality);
CREATE INDEX idx_transformations_modality ON transformations(modality, template_id, parent_id);
CREATE INDEX idx_edges_tx_state ON edges(transformation_id, state_id);
CREATE INDEX idx_edges_state ON edges(state_id);

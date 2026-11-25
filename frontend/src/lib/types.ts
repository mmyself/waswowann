export const CATEGORIES = ["Pilz", "Pflanze", "Person", "Ding", "Ereignis", "Unbekannt"] as const;
export type Category = (typeof CATEGORIES)[number];
export const ENVIRONMENTS = ["unknown", "indoor", "outdoor"] as const;
export type Environment = (typeof ENVIRONMENTS)[number];
export type Source = "demo" | "local" | "google_photos" | null;
export type ImportStatus = "queued" | "running" | "completed" | "partial" | "failed";
export type PlaceholderStatus = "not_requested" | "queued" | "running" | "ready" | "partial" | "error" | "unavailable";
export interface AnalysisResult {
  title?: string;
  category?: Category;
  description?: string;
  common_name?: string | null;
  scientific_name?: string | null;
  confidence?: number | null;
  tags?: string[];
  alternatives?: Array<{ name: string; scientific_name?: string | null; confidence?: number | null; rationale?: string }>;
  questions?: string[];
  warnings?: string[];
  estimated_sigma_meters?: number | null;
  estimated_sigma_seconds?: number | null;
  uncertainty_rationale?: string | null;
  environment?: Environment | null;
  apply_correction?: boolean;
}
export interface AnalysisHistoryItem {
  note_id: string;
  status: PlaceholderStatus;
  question_text?: string | null;
  text?: string;
  result?: AnalysisResult | null;
  created_at?: string | null;
  error_code?: string | null;
}

export interface TimeExtent {
  min: string;
  max: string;
}

export interface ObservationSummary {
  id: string;
  title: string;
  category: Category;
  environment?: Environment;
  lat: number | null;
  lon: number | null;
  observed_at: string | null;
  thumbnail_url: string | null;
  source: Source;
  media_count: number;
  location_measurement_count: number;
  first_observed_at: string | null;
  last_observed_at: string | null;
  sigma_meters: number | null;
  sigma_seconds: number | null;
}

export interface ObservationListResponse {
  items: ObservationSummary[];
  total: number;
  located_count: number;
  undated_count: number;
  time_extent: TimeExtent | null;
}

export interface SpaceTime {
  lat: number | null;
  lon: number | null;
  observed_at: string | null;
  sigma_meters: number | null;
  sigma_seconds: number | null;
  location_source: string;
  time_source: string;
  outlier_count?: number;
}

export interface MediaItem {
  id: string;
  filename: string;
  source: Exclude<Source, null>;
  width: number;
  height: number;
  image_url: string;
  thumbnail_url: string;
  exif: Record<string, string | number | null>;
  observed_at_ms: number | null;
  sigma_seconds: number | null;
  time_source: string;
  lat: number | null;
  lon: number | null;
  sigma_meters: number | null;
  location_source: string;
  effective_sigma_meters?: number | null;
  kalman_distance_meters?: number | null;
  kalman_z_score?: number | null;
  kalman_weight?: number | null;
  kalman_outlier?: boolean;
}

export type MediaMeasurementPatch = Partial<Pick<MediaItem, "observed_at_ms" | "sigma_seconds" | "time_source" | "lat" | "lon" | "sigma_meters" | "location_source">>;

export interface GeminiNote {
  status: PlaceholderStatus;
  source: "placeholder" | "takeout_import" | "gemini_api" | "google" | "antigravity";
  text: string;
  confidence: number | null;
  tags: string[];
  result?: AnalysisResult | null;
  alternatives?: AnalysisResult["alternatives"];
  warnings?: string[];
  error_code?: string | null;
  note_id?: string | null;
  history?: AnalysisHistoryItem[];
}

export interface MergeCandidate {
  observation_id: string;
  title: string;
  category: Category;
  observed_at: string | null;
  thumbnail_url: string | null;
  distance_meters: number;
  media_count: number;
}

export interface ObservationDetail extends ObservationSummary {
  description: string;
  created_at: string;
  updated_at: string;
  outlier_count?: number;
  space_time: SpaceTime;
  media: MediaItem[];
  gemini: GeminiNote;
}

export interface PublicConfig {
  google_photos_enabled: boolean;
  local_import_enabled: boolean;
  demo_enabled: boolean;
}

export interface ImportJob {
  id: string;
  source: Exclude<Source, null>;
  status: ImportStatus;
  total: number | null;
  processed: number;
  imported: number;
  skipped: number;
  failed: number;
  errors: Array<{ code: string; message: string }>;
}

export interface GoogleAuthStatus {
  enabled: boolean;
  authenticated: boolean;
  expires_at?: string | null;
}

export interface GooglePickerSession {
  id: string;
  status: "waiting" | "ready" | "importing" | "completed" | "cancelled" | "expired" | "failed";
  picker_url: string;
  poll_interval_ms: number;
  polling_deadline: string;
  expires_at: string;
}

export interface ApiErrorPayload {
  error?: { code?: string; message?: string };
  detail?: { error?: { code?: string; message?: string } };
}

export type GoalStatus = "on_track" | "at_risk" | "broken" | "cancelled";
export type StateModality = "observed" | "predicted" | "desired" | "cancelled";
export type TransformationModality = "happened" | "planned" | "template" | "cancelled";
export type EscalationAction = "widen_tolerance" | "switch_branch" | "abort_goal";

export interface MetaCoreEntity {
  id: string;
  name: string;
  kind: "worldline" | "type";
  category: string;
  source: "observed" | "derived";
  parent_entity_id: string | null;
  archived: boolean;
  meta: Record<string, unknown>;
  created_at_ms: number;
  updated_at_ms: number;
}

export interface MetaCoreState {
  id: string;
  entity_id: string;
  modality: StateModality;
  source: "measured" | "ai_prior" | "derived" | "user_goal";
  measurement_source: string;
  property_name: string;
  label: string;
  value_num: number | null;
  value_sigma: number | null;
  value_unit: string;
  value_text: string | null;
  lat: number | null;
  lon: number | null;
  sigma_meters: number | null;
  location_label: string;
  time_ms: number | null;
  sigma_seconds: number | null;
  target_min: number | null;
  target_max: number | null;
  target_latest_ms: number | null;
  target_radius_meters: number | null;
  priority: number;
  meta: Record<string, unknown>;
}

export interface MetaCoreTransformation {
  id: string;
  name: string;
  modality: TransformationModality;
  driver: "actor" | "nature";
  template_id: string | null;
  parent_id: string | null;
  duration_seconds: number | null;
  duration_sigma_seconds: number | null;
  duration_source: "measured" | "ai_prior" | "derived";
  cost: number;
  success_prob: number;
  formula: Record<string, unknown>;
  meta: Record<string, unknown>;
  created_at_ms: number;
}

export interface MetaCoreEdge {
  id: string;
  transformation_id: string;
  state_id: string;
  direction: "in" | "out";
  role: "consumes" | "uses" | "triggers" | "produces";
  logic_group: "AND" | "CHOICE_OR" | "WORLD_OR";
  branch_key: string;
  is_selected: boolean;
  outcome: string;
  probability: number | null;
  resource_released: boolean;
}

export interface BranchOption {
  branch_key: string;
  transformation_id: string;
  transformation_name: string;
  is_selected: boolean;
  cost: number;
  goal_probability: number;
  broken_dimensions: string[];
}

export interface GoalEvaluation {
  goal_id: string;
  entity_id: string;
  property_name: string;
  label: string;
  stage: number;
  status: GoalStatus;
  goal_probability: number;
  dimension_probs: Record<string, number>;
  broken_dimensions: string[];
  predicted_state_id: string | null;
  alternative_branches: BranchOption[];
  summary: string;
}

export interface ResourceConflict {
  entity_id: string;
  entity_name: string;
  transformation_a_id: string;
  transformation_a_name: string;
  transformation_b_id: string;
  transformation_b_name: string;
  overlap_start_ms: number;
  overlap_end_ms: number;
}

export interface EntityGeoFusion {
  entity_id: string;
  entity_name: string;
  observation_count: number;
  sources: string[];
  lat: number;
  lon: number;
  sigma_meters: number;
}

export interface SpatialPrediction {
  id: string;
  entity_id: string;
  entity_name: string;
  legacy_obs_id: string | null;
  property_name: string;
  label: string;
  lat: number;
  lon: number;
  sigma_meters: number;
  time_ms: number | null;
  sigma_seconds: number | null;
  source: string;
  value_num: number | null;
  value_sigma: number | null;
  value_unit: string;
  goal_id: string | null;
  goal_stage: number | null;
  goal_status: GoalStatus | null;
  goal_probability: number | null;
}

export interface SeasonalItem {
  id: string;
  kind: "observed" | "predicted";
  entity_id: string;
  obs_id: string | null;
  title: string;
  category: string;
  environment: string;
  day_of_year: number;
  sigma_days: number;
  year: number;
  date_label: string;
  time_ms: number;
  lat: number | null;
  lon: number | null;
  goal_status: GoalStatus | null;
  goal_probability: number | null;
}

export interface MutationFeedback {
  summary: string;
  created_state_ids: string[];
  updated_state_ids: string[];
  triggered_template_ids: string[];
  calibrated_template_ids: string[];
  goal_evaluations?: GoalEvaluation[];
}

export interface ZukunftsgraphSnapshot {
  title: string;
  subtitle: string;
  now_ms: number;
  synced_observation_count: number;
  entities: MetaCoreEntity[];
  states: MetaCoreState[];
  transformations: MetaCoreTransformation[];
  edges: MetaCoreEdge[];
  goal_evaluations: GoalEvaluation[];
  uses_conflicts: ResourceConflict[];
  open_ai_priors: string[];
  open_ai_prior_templates: string[];
  entity_geo_fusions: EntityGeoFusion[];
  spatial_predictions: SpatialPrediction[];
  seasonal_series: SeasonalItem[];
  last_feedback: MutationFeedback | null;
  current_clocks?: ClockPhaseSnapshot[];
  periodicity?: PeriodicityReport;
}

export interface WorldlineInspection {
  entity: MetaCoreEntity;
  legacy_obs_id: string | null;
  history_observed: MetaCoreState[];
  future_predicted: MetaCoreState[];
  goals_desired: MetaCoreState[];
  transformations: MetaCoreTransformation[];
  edges: MetaCoreEdge[];
  goal_evaluations: GoalEvaluation[];
  periodicity?: PeriodicityReport;
}

export type ClockId =
  | "day_local"
  | "solar_day"
  | "week_local"
  | "month_calendar"
  | "lunar_synodic"
  | "tidal_semidiurnal"
  | "year_solar"
  | "relative_interval";

export type PeriodStatus = "detected" | "weak" | "none" | "insufficient_data";

export interface ClockPhaseSnapshot {
  clock_id: ClockId;
  clock_name: string;
  phase: number;
  cycle_index: number;
  period_seconds: number;
  label: string;
  detail: string;
  solar_elevation_deg?: number | null;
  is_daylight?: boolean | null;
  lunar_illumination?: number | null;
  is_spring_tide?: boolean | null;
}

export interface PeriodCandidate {
  clock_id: ClockId;
  clock_name: string;
  status: PeriodStatus;
  period_seconds: number;
  sample_count: number;
  covered_cycles: number;
  concentration: number;
  p_value?: number | null;
  mean_phase?: number | null;
  sigma_phase?: number | null;
  mean_interval_seconds?: number | null;
  sigma_interval_seconds?: number | null;
  mean_label: string;
  next_window_start_ms?: number | null;
  next_window_center_ms?: number | null;
  next_window_end_ms?: number | null;
  explanation: string;
}

export interface PeriodicityReport {
  entity_id?: string | null;
  label: string;
  now_ms: number;
  sample_count: number;
  distinct_event_count: number;
  current_clocks: ClockPhaseSnapshot[];
  candidates: PeriodCandidate[];
  best_clock_id?: ClockId | null;
  summary: string;
}

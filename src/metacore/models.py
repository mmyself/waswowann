"""Strict Pydantic v2 domain models for MetaCore entities, states, transformations, and edges."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EntityKind = Literal["worldline", "type"]
EntitySource = Literal["observed", "derived"]
StateModality = Literal["observed", "predicted", "desired", "cancelled"]
ValueSource = Literal["measured", "ai_prior", "derived", "user_goal"]
DistributionKind = Literal["normal", "discrete", "point"]
TransformationModality = Literal["happened", "planned", "template", "cancelled"]
TransformationDriver = Literal["actor", "nature"]
DurationSource = Literal["measured", "ai_prior", "derived"]
EdgeDirection = Literal["in", "out"]
EdgeRole = Literal["consumes", "uses", "triggers", "produces"]
LogicGroup = Literal["AND", "CHOICE_OR", "WORLD_OR"]
EdgeOutcome = Literal["pending", "chosen", "not_chosen", "occurred", "did_not_occur"]


class StrictModel(BaseModel):
    """Base model forbidding extra fields and non-finite floats."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _strip_non_empty(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} darf nicht leer sein")
    return stripped


class EntityCreate(StrictModel):
    """Input payload for creating a worldline or interchangeable type entity."""

    id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    kind: EntityKind
    category: str = Field(default="general", min_length=1, max_length=80)
    source: EntitySource = "observed"
    parent_entity_id: str | None = Field(default=None, min_length=1, max_length=128)
    archived: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "category")
    @classmethod
    def check_non_empty(cls, value: str, info: Any) -> str:
        return _strip_non_empty(value, info.field_name)


class Entity(StrictModel):
    """Persisted worldline or type entity."""

    id: str
    name: str
    kind: EntityKind
    category: str
    source: EntitySource
    parent_entity_id: str | None
    archived: bool
    meta: dict[str, Any]
    created_at_ms: int
    updated_at_ms: int


class StateCreate(StrictModel):
    """Input payload for creating a property-level state or goal."""

    id: str | None = Field(default=None, min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=128)
    modality: StateModality
    source: ValueSource
    measurement_source: str = Field(default="", max_length=200)
    property_name: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=240)

    value_num: float | None = None
    value_sigma: float | None = Field(default=None, ge=0.0)
    value_unit: str = Field(default="", max_length=40)
    value_text: str | None = Field(default=None, max_length=500)
    distribution_kind: DistributionKind = "normal"

    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    sigma_meters: float | None = Field(default=None, gt=0.0)
    location_label: str = Field(default="", max_length=200)

    time_ms: int | None = None
    sigma_seconds: float | None = Field(default=None, gt=0.0)

    target_min: float | None = None
    target_max: float | None = None
    target_preferred: float | None = None
    target_value_text: str | None = Field(default=None, max_length=500)
    target_earliest_ms: int | None = None
    target_latest_ms: int | None = None
    target_radius_meters: float | None = Field(default=None, gt=0.0)
    threshold_ok: float = Field(default=0.8, ge=0.0, le=1.0)
    threshold_fail: float = Field(default=0.2, ge=0.0, le=1.0)
    priority: float = Field(default=1.0, ge=0.0)

    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("property_name", "label")
    @classmethod
    def check_non_empty(cls, value: str, info: Any) -> str:
        return _strip_non_empty(value, info.field_name)

    @model_validator(mode="after")
    def validate_state_invariants(self) -> StateCreate:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat und lon müssen gemeinsam gesetzt oder leer sein")
        if self.value_sigma is not None and self.value_num is None:
            raise ValueError("value_sigma erfordert value_num")
        if self.sigma_meters is not None and self.lat is None:
            raise ValueError("sigma_meters erfordert Koordinaten (lat, lon)")
        if self.sigma_seconds is not None and self.time_ms is None:
            raise ValueError("sigma_seconds erfordert time_ms")
        if self.modality == "desired" and any(
            v is not None for v in (self.value_sigma, self.sigma_meters, self.sigma_seconds)
        ):
            raise ValueError("Ziele (desired) tragen Ziel-Grenzen und keine sigma-Unsicherheitsfelder")
        if (
            self.target_min is not None
            and self.target_max is not None
            and self.target_min > self.target_max
        ):
            raise ValueError("target_min darf nicht größer als target_max sein")
        if (
            self.target_earliest_ms is not None
            and self.target_latest_ms is not None
            and self.target_earliest_ms > self.target_latest_ms
        ):
            raise ValueError("target_earliest_ms darf nicht nach target_latest_ms liegen")
        if self.threshold_fail > self.threshold_ok:
            raise ValueError("threshold_fail darf nicht größer als threshold_ok sein")
        return self


class State(StrictModel):
    """Persisted property-level state or goal."""

    id: str
    entity_id: str
    modality: StateModality
    source: ValueSource
    measurement_source: str
    property_name: str
    label: str

    value_num: float | None
    value_sigma: float | None
    value_unit: str
    value_text: str | None
    distribution_kind: DistributionKind

    lat: float | None
    lon: float | None
    sigma_meters: float | None
    location_label: str

    time_ms: int | None
    sigma_seconds: float | None

    target_min: float | None
    target_max: float | None
    target_preferred: float | None
    target_value_text: str | None
    target_earliest_ms: int | None
    target_latest_ms: int | None
    target_radius_meters: float | None
    threshold_ok: float
    threshold_fail: float
    priority: float

    meta: dict[str, Any]
    created_at_ms: int
    updated_at_ms: int


class TransformationCreate(StrictModel):
    """Input payload for creating an action, passive nature process, or template."""

    id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    modality: TransformationModality
    driver: TransformationDriver
    template_id: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: str | None = Field(default=None, min_length=1, max_length=128)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    duration_sigma_seconds: float | None = Field(default=None, gt=0.0)
    duration_source: DurationSource = "ai_prior"
    cost: float = Field(default=0.0, ge=0.0)
    success_prob: float = Field(default=1.0, gt=0.0, le=1.0)
    formula: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def check_non_empty(cls, value: str, info: Any) -> str:
        return _strip_non_empty(value, info.field_name)

    @model_validator(mode="after")
    def validate_duration(self) -> TransformationCreate:
        if self.duration_sigma_seconds is not None and self.duration_seconds is None:
            raise ValueError("duration_sigma_seconds erfordert duration_seconds")
        return self


class Transformation(StrictModel):
    """Persisted transformation, template, or composite subgraph node."""

    id: str
    name: str
    modality: TransformationModality
    driver: TransformationDriver
    template_id: str | None
    parent_id: str | None
    duration_seconds: float | None
    duration_sigma_seconds: float | None
    duration_source: DurationSource
    cost: float
    success_prob: float
    formula: dict[str, Any]
    meta: dict[str, Any]
    created_at_ms: int
    updated_at_ms: int


class EdgeCreate(StrictModel):
    """Input payload for connecting a state and a transformation."""

    id: str | None = Field(default=None, min_length=1, max_length=128)
    transformation_id: str = Field(min_length=1, max_length=128)
    state_id: str = Field(min_length=1, max_length=128)
    direction: EdgeDirection
    role: EdgeRole
    logic_group: LogicGroup = "AND"
    branch_key: str = Field(default="default", min_length=1, max_length=120)
    is_selected: bool = True
    outcome: EdgeOutcome = "pending"
    probability: float | None = Field(default=None, gt=0.0, le=1.0)
    ratio_mean: float | None = None
    ratio_sigma: float | None = Field(default=None, ge=0.0)
    resource_released: bool = False

    @model_validator(mode="after")
    def validate_ratio(self) -> EdgeCreate:
        if self.ratio_sigma is not None and self.ratio_mean is None:
            raise ValueError("ratio_sigma erfordert ratio_mean")
        return self


class Edge(StrictModel):
    """Persisted directed edge between a state and a transformation."""

    id: str
    transformation_id: str
    state_id: str
    direction: EdgeDirection
    role: EdgeRole
    logic_group: LogicGroup
    branch_key: str
    is_selected: bool
    outcome: EdgeOutcome
    probability: float | None
    ratio_mean: float | None
    ratio_sigma: float | None
    resource_released: bool
    created_at_ms: int

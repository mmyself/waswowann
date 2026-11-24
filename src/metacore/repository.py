"""SQLite repository layer for entities, property states, transformations, and edges."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from metacore.config import Settings, default_settings
from metacore.db import db, now_ms
from metacore.models import (
    Edge,
    EdgeCreate,
    EdgeDirection,
    EdgeRole,
    Entity,
    EntityCreate,
    State,
    StateCreate,
    StateModality,
    Transformation,
    TransformationCreate,
    TransformationModality,
)


def _new_id(prefix: str, explicit: str | None) -> str:
    return explicit if explicit is not None else f"{prefix}_{uuid4().hex[:12]}"


def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        category=row["category"],
        source=row["source"],
        parent_entity_id=row["parent_entity_id"],
        archived=bool(row["archived"]),
        meta=json.loads(row["meta_json"]),
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_state(row: sqlite3.Row) -> State:
    return State(
        id=row["id"],
        entity_id=row["entity_id"],
        modality=row["modality"],
        source=row["source"],
        measurement_source=row["measurement_source"],
        property_name=row["property_name"],
        label=row["label"],
        value_num=row["value_num"],
        value_sigma=row["value_sigma"],
        value_unit=row["value_unit"],
        value_text=row["value_text"],
        distribution_kind=row["distribution_kind"],
        lat=row["lat"],
        lon=row["lon"],
        sigma_meters=row["sigma_meters"],
        location_label=row["location_label"],
        time_ms=row["time_ms"],
        sigma_seconds=row["sigma_seconds"],
        target_min=row["target_min"],
        target_max=row["target_max"],
        target_preferred=row["target_preferred"],
        target_value_text=row["target_value_text"],
        target_earliest_ms=row["target_earliest_ms"],
        target_latest_ms=row["target_latest_ms"],
        target_radius_meters=row["target_radius_meters"],
        threshold_ok=row["threshold_ok"],
        threshold_fail=row["threshold_fail"],
        priority=row["priority"],
        meta=json.loads(row["meta_json"]),
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_transformation(row: sqlite3.Row) -> Transformation:
    return Transformation(
        id=row["id"],
        name=row["name"],
        modality=row["modality"],
        driver=row["driver"],
        template_id=row["template_id"],
        parent_id=row["parent_id"],
        duration_seconds=row["duration_seconds"],
        duration_sigma_seconds=row["duration_sigma_seconds"],
        duration_source=row["duration_source"],
        cost=row["cost"],
        success_prob=row["success_prob"],
        formula=json.loads(row["formula_json"]),
        meta=json.loads(row["meta_json"]),
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_edge(row: sqlite3.Row) -> Edge:
    return Edge(
        id=row["id"],
        transformation_id=row["transformation_id"],
        state_id=row["state_id"],
        direction=row["direction"],
        role=row["role"],
        logic_group=row["logic_group"],
        branch_key=row["branch_key"],
        is_selected=bool(row["is_selected"]),
        outcome=row["outcome"],
        probability=row["probability"],
        ratio_mean=row["ratio_mean"],
        ratio_sigma=row["ratio_sigma"],
        resource_released=bool(row["resource_released"]),
        created_at_ms=row["created_at_ms"],
    )


class MetaCoreRepository:
    """Thin typed persistence access over the 4 MetaCore SQLite tables."""

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or default_settings

    # --- Entities ---

    def create_entity(self, data: EntityCreate) -> Entity:
        """Persist a new worldline or interchangeable type entity."""
        entity_id = _new_id("ent", data.id)
        ts = now_ms()
        with db(self.config) as conn:
            conn.execute(
                """
                INSERT INTO entities (
                    id, name, kind, category, source, parent_entity_id,
                    archived, meta_json, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    data.name,
                    data.kind,
                    data.category,
                    data.source,
                    data.parent_entity_id,
                    int(data.archived),
                    json.dumps(data.meta),
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return _row_to_entity(row)

    def get_entity(self, entity_id: str) -> Entity | None:
        """Fetch an entity by ID or return None."""
        with db(self.config) as conn:
            row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return _row_to_entity(row) if row else None

    def list_entities(self, *, include_archived: bool = False) -> list[Entity]:
        """List entities ordered by creation time."""
        query = "SELECT * FROM entities"
        if not include_archived:
            query += " WHERE archived = 0"
        query += " ORDER BY created_at_ms, rowid"
        with db(self.config) as conn:
            rows = conn.execute(query).fetchall()
        return [_row_to_entity(r) for r in rows]

    def find_entities(
        self,
        query: str = "",
        *,
        category: str | None = None,
        include_archived: bool = False,
    ) -> list[Entity]:
        """Search entities by case-insensitive name substring and optional category."""
        clauses: list[str] = []
        params: list[object] = []
        if not include_archived:
            clauses.append("archived = 0")
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        needle = query.strip().lower()
        if needle:
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("LOWER(name) LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM entities{where} ORDER BY created_at_ms, rowid"
        with db(self.config) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_entity(r) for r in rows]

    # --- States ---

    def create_state(self, data: StateCreate) -> State:
        """Persist a new property-level state or goal."""
        state_id = _new_id("st", data.id)
        ts = now_ms()
        with db(self.config) as conn:
            conn.execute(
                """
                INSERT INTO states (
                    id, entity_id, modality, source, measurement_source, property_name, label,
                    value_num, value_sigma, value_unit, value_text, distribution_kind,
                    lat, lon, sigma_meters, location_label,
                    time_ms, sigma_seconds,
                    target_min, target_max, target_preferred, target_value_text,
                    target_earliest_ms, target_latest_ms, target_radius_meters,
                    threshold_ok, threshold_fail, priority,
                    meta_json, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state_id,
                    data.entity_id,
                    data.modality,
                    data.source,
                    data.measurement_source,
                    data.property_name,
                    data.label,
                    data.value_num,
                    data.value_sigma,
                    data.value_unit,
                    data.value_text,
                    data.distribution_kind,
                    data.lat,
                    data.lon,
                    data.sigma_meters,
                    data.location_label,
                    data.time_ms,
                    data.sigma_seconds,
                    data.target_min,
                    data.target_max,
                    data.target_preferred,
                    data.target_value_text,
                    data.target_earliest_ms,
                    data.target_latest_ms,
                    data.target_radius_meters,
                    data.threshold_ok,
                    data.threshold_fail,
                    data.priority,
                    json.dumps(data.meta),
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM states WHERE id = ?", (state_id,)).fetchone()
        return _row_to_state(row)

    def get_state(self, state_id: str) -> State | None:
        """Fetch a state by ID or return None."""
        with db(self.config) as conn:
            row = conn.execute("SELECT * FROM states WHERE id = ?", (state_id,)).fetchone()
        return _row_to_state(row) if row else None

    def list_states(
        self,
        *,
        entity_id: str | None = None,
        property_name: str | None = None,
        modality: StateModality | None = None,
    ) -> list[State]:
        """List states optionally filtered by entity, property name, or modality."""
        clauses: list[str] = []
        params: list[object] = []
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if property_name is not None:
            clauses.append("property_name = ?")
            params.append(property_name)
        if modality is not None:
            clauses.append("modality = ?")
            params.append(modality)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM states{where} ORDER BY time_ms IS NULL, time_ms, created_at_ms, rowid"
        with db(self.config) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_state(r) for r in rows]

    def update_state(self, state_id: str, **updates: Any) -> State:
        """Update fields on an existing state while enforcing all StateCreate invariants."""
        current = self.get_state(state_id)
        if current is None:
            raise KeyError(f"Unbekannter Zustand: {state_id}")
        payload = current.model_dump(exclude={"created_at_ms", "updated_at_ms"})
        payload.update(updates)
        validated = StateCreate(**payload)
        ts = now_ms()
        with db(self.config) as conn:
            conn.execute(
                """
                UPDATE states SET
                    entity_id = ?, modality = ?, source = ?, measurement_source = ?,
                    property_name = ?, label = ?,
                    value_num = ?, value_sigma = ?, value_unit = ?, value_text = ?,
                    distribution_kind = ?,
                    lat = ?, lon = ?, sigma_meters = ?, location_label = ?,
                    time_ms = ?, sigma_seconds = ?,
                    target_min = ?, target_max = ?, target_preferred = ?, target_value_text = ?,
                    target_earliest_ms = ?, target_latest_ms = ?, target_radius_meters = ?,
                    threshold_ok = ?, threshold_fail = ?, priority = ?,
                    meta_json = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    validated.entity_id,
                    validated.modality,
                    validated.source,
                    validated.measurement_source,
                    validated.property_name,
                    validated.label,
                    validated.value_num,
                    validated.value_sigma,
                    validated.value_unit,
                    validated.value_text,
                    validated.distribution_kind,
                    validated.lat,
                    validated.lon,
                    validated.sigma_meters,
                    validated.location_label,
                    validated.time_ms,
                    validated.sigma_seconds,
                    validated.target_min,
                    validated.target_max,
                    validated.target_preferred,
                    validated.target_value_text,
                    validated.target_earliest_ms,
                    validated.target_latest_ms,
                    validated.target_radius_meters,
                    validated.threshold_ok,
                    validated.threshold_fail,
                    validated.priority,
                    json.dumps(validated.meta),
                    ts,
                    state_id,
                ),
            )
            row = conn.execute("SELECT * FROM states WHERE id = ?", (state_id,)).fetchone()
        return _row_to_state(row)

    # --- Transformations ---

    def create_transformation(self, data: TransformationCreate) -> Transformation:
        """Persist a new transformation, passive process, or template."""
        tx_id = _new_id("tx", data.id)
        ts = now_ms()
        with db(self.config) as conn:
            conn.execute(
                """
                INSERT INTO transformations (
                    id, name, modality, driver, template_id, parent_id,
                    duration_seconds, duration_sigma_seconds, duration_source,
                    cost, success_prob, formula_json, meta_json, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_id,
                    data.name,
                    data.modality,
                    data.driver,
                    data.template_id,
                    data.parent_id,
                    data.duration_seconds,
                    data.duration_sigma_seconds,
                    data.duration_source,
                    data.cost,
                    data.success_prob,
                    json.dumps(data.formula),
                    json.dumps(data.meta),
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM transformations WHERE id = ?", (tx_id,)).fetchone()
        return _row_to_transformation(row)

    def get_transformation(self, transformation_id: str) -> Transformation | None:
        """Fetch a transformation by ID or return None."""
        with db(self.config) as conn:
            row = conn.execute(
                "SELECT * FROM transformations WHERE id = ?", (transformation_id,)
            ).fetchone()
        return _row_to_transformation(row) if row else None

    def list_transformations(
        self, *, modality: TransformationModality | None = None
    ) -> list[Transformation]:
        """List transformations optionally filtered by modality."""
        query = "SELECT * FROM transformations"
        params: list[object] = []
        if modality is not None:
            query += " WHERE modality = ?"
            params.append(modality)
        query += " ORDER BY created_at_ms, rowid"
        with db(self.config) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_transformation(r) for r in rows]

    def update_transformation(self, transformation_id: str, **updates: Any) -> Transformation:
        """Update fields on an existing transformation while validating invariants."""
        current = self.get_transformation(transformation_id)
        if current is None:
            raise KeyError(f"Unbekannte Transformation: {transformation_id}")
        payload = current.model_dump(exclude={"created_at_ms", "updated_at_ms"})
        payload.update(updates)
        validated = TransformationCreate(**payload)
        ts = now_ms()
        with db(self.config) as conn:
            conn.execute(
                """
                UPDATE transformations SET
                    name = ?, modality = ?, driver = ?, template_id = ?, parent_id = ?,
                    duration_seconds = ?, duration_sigma_seconds = ?, duration_source = ?,
                    cost = ?, success_prob = ?, formula_json = ?, meta_json = ?,
                    updated_at_ms = ?
                WHERE id = ?
                """,
                (
                    validated.name,
                    validated.modality,
                    validated.driver,
                    validated.template_id,
                    validated.parent_id,
                    validated.duration_seconds,
                    validated.duration_sigma_seconds,
                    validated.duration_source,
                    validated.cost,
                    validated.success_prob,
                    json.dumps(validated.formula),
                    json.dumps(validated.meta),
                    ts,
                    transformation_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM transformations WHERE id = ?", (transformation_id,)
            ).fetchone()
        return _row_to_transformation(row)

    # --- Edges ---

    def create_edge(self, data: EdgeCreate) -> Edge:
        """Persist a directed causal or resource edge between a state and a transformation."""
        edge_id = _new_id("edg", data.id)
        ts = now_ms()
        with db(self.config) as conn:
            conn.execute(
                """
                INSERT INTO edges (
                    id, transformation_id, state_id, direction, role, logic_group,
                    branch_key, is_selected, outcome, probability, ratio_mean, ratio_sigma,
                    resource_released, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge_id,
                    data.transformation_id,
                    data.state_id,
                    data.direction,
                    data.role,
                    data.logic_group,
                    data.branch_key,
                    int(data.is_selected),
                    data.outcome,
                    data.probability,
                    data.ratio_mean,
                    data.ratio_sigma,
                    int(data.resource_released),
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
        return _row_to_edge(row)

    def get_edge(self, edge_id: str) -> Edge | None:
        """Fetch an edge by ID or return None."""
        with db(self.config) as conn:
            row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
        return _row_to_edge(row) if row else None

    def list_edges(
        self,
        *,
        transformation_id: str | None = None,
        state_id: str | None = None,
        direction: EdgeDirection | None = None,
        role: EdgeRole | None = None,
    ) -> list[Edge]:
        """List edges optionally filtered by transformation, state, direction, or role."""
        clauses: list[str] = []
        params: list[object] = []
        if transformation_id is not None:
            clauses.append("transformation_id = ?")
            params.append(transformation_id)
        if state_id is not None:
            clauses.append("state_id = ?")
            params.append(state_id)
        if direction is not None:
            clauses.append("direction = ?")
            params.append(direction)
        if role is not None:
            clauses.append("role = ?")
            params.append(role)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM edges{where} ORDER BY created_at_ms, rowid"
        with db(self.config) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    def update_edge(self, edge_id: str, **updates: Any) -> Edge:
        """Update fields on an existing edge while validating invariants."""
        current = self.get_edge(edge_id)
        if current is None:
            raise KeyError(f"Unbekannte Kante: {edge_id}")
        payload = current.model_dump(exclude={"created_at_ms"})
        payload.update(updates)
        validated = EdgeCreate(**payload)
        with db(self.config) as conn:
            conn.execute(
                """
                UPDATE edges SET
                    transformation_id = ?, state_id = ?, direction = ?, role = ?,
                    logic_group = ?, branch_key = ?, is_selected = ?, outcome = ?,
                    probability = ?, ratio_mean = ?, ratio_sigma = ?, resource_released = ?
                WHERE id = ?
                """,
                (
                    validated.transformation_id,
                    validated.state_id,
                    validated.direction,
                    validated.role,
                    validated.logic_group,
                    validated.branch_key,
                    int(validated.is_selected),
                    validated.outcome,
                    validated.probability,
                    validated.ratio_mean,
                    validated.ratio_sigma,
                    int(validated.resource_released),
                    edge_id,
                ),
            )
            row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
        return _row_to_edge(row)

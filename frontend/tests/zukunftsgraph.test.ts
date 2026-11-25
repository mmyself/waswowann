import { describe, expect, it } from "vitest";
import { predictedPointFeatures, predictedUncertaintyFeatures } from "../src/lib/contours";
import { mergeFutureExtent } from "../src/lib/time-filter";
import { buildCausalSvgLayout, buildTransformationHierarchy, computeCausalSteps, formatGoalBounds, periodStatusLabel } from "../src/lib/zukunftsgraph";
import type { GoalEvaluation, MetaCoreEdge, MetaCoreState, MetaCoreTransformation, SpatialPrediction } from "../src/lib/types";

describe("MetaCore frontend helpers", () => {
  it("builds dashed predicted uncertainty rings and points", () => {
    const preds: SpatialPrediction[] = [
      {
        id: "pred-1",
        entity_id: "ent:habitat",
        entity_name: "Myzel-Habitat",
        legacy_obs_id: "obs-1",
        property_name: "presence",
        label: "Herbstschub",
        lat: 53.558,
        lon: 9.811,
        sigma_meters: 45,
        time_ms: 1_796_000_000_000,
        sigma_seconds: 86_400,
        source: "derived",
        value_num: 3,
        value_sigma: 1,
        value_unit: "stk",
        goal_id: "goal-1",
        goal_stage: 2,
        goal_status: "on_track",
        goal_probability: 0.91,
      },
    ];
    const rings = predictedUncertaintyFeatures(preds);
    const pts = predictedPointFeatures(preds);
    expect(rings.features.map((f) => f.properties.level)).toEqual([68, 95]);
    expect(rings.features[0].properties.status).toBe("on_track");
    expect(pts.features[0].properties.legacy_obs_id).toBe("obs-1");
  });

  it("extends observed time windows cleanly into future predictions", () => {
    const base = { start: 1_795_000_000_000, end: 1_795_500_000_000 };
    const merged = mergeFutureExtent(base, [1_796_200_000_000, 1_796_500_000_000]);
    expect(merged).toEqual({ start: 1_795_000_000_000, end: 1_796_500_000_000 });
    expect(mergeFutureExtent(base, [])).toEqual(base);
  });

  it("groups nested child transformations under parent_id and computes causal order", () => {
    const txs: MetaCoreTransformation[] = [
      {
        id: "tx-parent",
        name: "Substrat herstellen",
        modality: "planned",
        driver: "actor",
        template_id: null,
        parent_id: null,
        duration_seconds: 86_400,
        duration_sigma_seconds: null,
        duration_source: "ai_prior",
        cost: 0,
        success_prob: 0.95,
        formula: {},
        meta: { step_order: 1 },
        created_at_ms: 1,
      },
      {
        id: "tx-child-2",
        name: "2. Autoklav",
        modality: "planned",
        driver: "actor",
        template_id: null,
        parent_id: "tx-parent",
        duration_seconds: 3600,
        duration_sigma_seconds: null,
        duration_source: "measured",
        cost: 0,
        success_prob: 0.98,
        formula: {},
        meta: { step_order: 2 },
        created_at_ms: 2,
      },
      {
        id: "tx-child-1",
        name: "1. Einweichen",
        modality: "planned",
        driver: "actor",
        template_id: null,
        parent_id: "tx-parent",
        duration_seconds: 7200,
        duration_sigma_seconds: null,
        duration_source: "measured",
        cost: 0,
        success_prob: 0.99,
        formula: {},
        meta: { step_order: 1 },
        created_at_ms: 3,
      },
      {
        id: "tx-downstream",
        name: "Fruchtung",
        modality: "planned",
        driver: "nature",
        template_id: null,
        parent_id: null,
        duration_seconds: 86_400,
        duration_sigma_seconds: null,
        duration_source: "ai_prior",
        cost: 0,
        success_prob: 0.9,
        formula: {},
        meta: {},
        created_at_ms: 4,
      },
    ];
    const states: MetaCoreState[] = [
      {
        id: "st-mid",
        entity_id: "ent-1",
        modality: "predicted",
        source: "derived",
        measurement_source: "",
        property_name: "block",
        label: "Substratblock",
        value_num: 1000,
        value_sigma: 50,
        value_unit: "g",
        value_text: null,
        lat: null,
        lon: null,
        sigma_meters: null,
        location_label: "",
        time_ms: null,
        sigma_seconds: null,
        target_min: null,
        target_max: null,
        target_latest_ms: null,
        target_radius_meters: null,
        priority: 1,
        meta: {},
      },
    ];
    const edges: MetaCoreEdge[] = [
      {
        id: "e1",
        transformation_id: "tx-parent",
        state_id: "st-mid",
        direction: "out",
        role: "produces",
        logic_group: "AND",
        branch_key: "default",
        is_selected: true,
        outcome: "pending",
        probability: null,
        resource_released: false,
      },
      {
        id: "e2",
        transformation_id: "tx-downstream",
        state_id: "st-mid",
        direction: "in",
        role: "consumes",
        logic_group: "AND",
        branch_key: "default",
        is_selected: true,
        outcome: "pending",
        probability: null,
        resource_released: false,
      },
    ];

    const h = buildTransformationHierarchy(txs);
    expect(h.roots.map((r) => r.id)).toEqual(["tx-parent", "tx-downstream"]);
    expect(h.childrenByParent["tx-parent"].map((c) => c.id)).toEqual(["tx-child-1", "tx-child-2"]);

    const causal = computeCausalSteps(txs, states, edges);
    expect(causal.orderedRoots.map((r) => r.id)).toEqual(["tx-parent", "tx-downstream"]);
    expect(periodStatusLabel("detected")).toBe("Erkannt");
    expect(periodStatusLabel("insufficient_data")).toBe("Zu wenig Zyklen");

    const goalState: MetaCoreState = {
      ...states[0],
      id: "goal-1",
      modality: "desired",
      source: "user_goal",
      label: "Ziel Block",
      value_num: null,
      value_sigma: null,
      target_min: 900,
      target_max: 1200,
      target_earliest_ms: 1_795_500_000_000,
      target_latest_ms: 1_795_759_200_000,
    };
    const boundsLabel = formatGoalBounds(goalState);
    expect(boundsLabel).toContain("≥ 900 g");
    expect(boundsLabel).toContain("≤ 1200 g");
    expect(boundsLabel).toContain("–");

    const evals: GoalEvaluation[] = [
      {
        goal_id: "goal-1",
        entity_id: "ent-1",
        property_name: "block",
        label: "Ziel Block",
        stage: 4,
        status: "broken",
        goal_probability: 0.05,
        dimension_probs: { wie_viel: 0.9, wann: 0.05 },
        broken_dimensions: ["wann"],
        predicted_state_id: "st-mid",
        alternative_branches: [],
        summary: "wann gebrochen",
      },
    ];
    const layout = buildCausalSvgLayout(txs, [...states, goalState], edges, evals);
    const goalNode = layout.nodes.find((n) => n.id === "goal-1");
    expect(goalNode?.status).toBe("broken");
    expect(goalNode?.sublabel).toContain("≥ 900 g");
    expect(layout.links.some((l) => l.role === "prüft" && l.status === "broken")).toBe(true);
  });
});

import { describe, expect, it } from "vitest";

import type { ProductWorkflow, WorkflowEdge, WorkflowNode } from "../../lib/types";
import {
  createRestoreNodesStep,
  getInternalWorkflowEdges,
  getWorkflowStructureSignature,
  workflowHistoryStepRequiresConfirmation,
} from "./workflowHistory";

const baseNode = (id: string, overrides: Partial<WorkflowNode> = {}): WorkflowNode => ({
  id,
  workflow_id: "workflow",
  node_type: "copy_generation",
  title: id,
  position_x: 120,
  position_y: 160,
  config_json: { instruction: "keep" },
  status: "succeeded",
  output_json: { copy_set_id: "copy-1" },
  failure_reason: "old failure",
  is_retryable: false,
  attempt_count: 0,
  retry_count: 0,
  non_retryable_reason: null,
  retry_hint: null,
  last_run_at: "2026-05-14T01:02:03Z",
  created_at: "2026-05-14T01:02:03Z",
  updated_at: "2026-05-14T01:02:03Z",
  ...overrides,
});

const baseEdge = (id: string, source: string, target: string): WorkflowEdge => ({
  id,
  workflow_id: "workflow",
  source_node_id: source,
  target_node_id: target,
  source_handle: "output",
  target_handle: "input",
  created_at: "2026-05-14T01:02:03Z",
});

const baseWorkflow = (overrides: Partial<ProductWorkflow> = {}): ProductWorkflow => ({
  id: "workflow",
  product_id: "product",
  title: "Workflow",
  active: true,
  nodes: [baseNode("b"), baseNode("a")],
  edges: [baseEdge("edge-2", "b", "a"), baseEdge("edge-1", "a", "b")],
  runs: [],
  created_at: "2026-05-14T01:02:03Z",
  updated_at: "2026-05-14T01:02:03Z",
  ...overrides,
});

describe("workflow history helpers", () => {
  it("only asks for confirmation when executing a history step would delete nodes or edges", () => {
    expect(workflowHistoryStepRequiresConfirmation({ kind: "deleteNodes", nodeIds: ["a"] })).toBe(true);
    expect(workflowHistoryStepRequiresConfirmation({ kind: "deleteEdges", edgeIds: ["edge-1"] })).toBe(true);
    expect(workflowHistoryStepRequiresConfirmation({ kind: "restoreNodes", nodes: [], edges: [] })).toBe(false);
    expect(workflowHistoryStepRequiresConfirmation({ kind: "restoreEdges", edges: [] })).toBe(false);
    expect(workflowHistoryStepRequiresConfirmation({ kind: "moveNodes", moves: [] })).toBe(false);
  });

  it("keeps only edges internal to the selected nodes when restoring a deletion", () => {
    const edges = [
      baseEdge("internal", "a", "b"),
      baseEdge("incoming", "outside", "a"),
      baseEdge("outgoing", "b", "outside"),
    ];

    expect(getInternalWorkflowEdges(edges, new Set(["a", "b"])).map((edge) => edge.id)).toEqual(["internal"]);
  });

  it("builds a stable structure signature from workflow timestamp and sorted structure ids", () => {
    expect(getWorkflowStructureSignature(baseWorkflow())).toBe(
      "workflow|2026-05-14T01:02:03Z|a,b|edge-1,edge-2",
    );
    expect(
      getWorkflowStructureSignature(
        baseWorkflow({
          updated_at: "2026-05-14T02:00:00Z",
          nodes: [baseNode("a"), baseNode("c")],
          edges: [baseEdge("edge-3", "a", "c")],
        }),
      ),
    ).toBe("workflow|2026-05-14T02:00:00Z|a,c|edge-3");
  });

  it("stores deleted node structure without output, run state, or generated artifacts", () => {
    const step = createRestoreNodesStep(
      [
        baseNode("a", {
          config_json: {
            instruction: "keep",
            copy_set_id: "copy-1",
            nested: { poster_variant_id: "poster-1", keep: "value" },
          },
        }),
        baseNode("b", {
          node_type: "image_generation",
          config_json: {
            instruction: "keep",
            generated_poster_variant_ids: ["poster-1"],
            filled_source_asset_ids: ["asset-1"],
            preview_url: "/api/asset",
          },
        }),
      ],
      [baseEdge("internal", "a", "b")],
    );

    expect(step).toEqual({
      kind: "restoreNodes",
      nodes: [
        {
          oldId: "a",
          node_type: "copy_generation",
          title: "a",
          position_x: 120,
          position_y: 160,
          config_json: { instruction: "keep", nested: { keep: "value" } },
        },
        {
          oldId: "b",
          node_type: "image_generation",
          title: "b",
          position_x: 120,
          position_y: 160,
          config_json: { instruction: "keep" },
        },
      ],
      edges: [
        {
          oldId: "internal",
          source_node_id: "a",
          target_node_id: "b",
          source_handle: "output",
          target_handle: "input",
        },
      ],
    });
  });
});

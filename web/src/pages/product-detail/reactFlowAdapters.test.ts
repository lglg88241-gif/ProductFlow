import { describe, expect, it } from "vitest";

import type { ProductWorkflow, WorkflowEdge, WorkflowNode } from "../../lib/types";
import {
  PRODUCTFLOW_EDGE_TYPE,
  PRODUCTFLOW_NODE_TYPE,
  PRODUCTFLOW_SOURCE_HANDLE,
  PRODUCTFLOW_TARGET_HANDLE,
  connectionToWorkflowEdgeInput,
  getChangedWorkflowNodePositionCandidates,
  getNodeDragGroupScreenDistance,
  getNodePositionForViewportCenter,
  normalizeWorkflowZoom,
  reactFlowPositionToWorkflowPatch,
  shouldCommitNodeDragGroupPosition,
  workflowEdgeIdFromReactFlowEdge,
  workflowNodeIdFromReactFlowNode,
  workflowToReactFlowEdges,
  workflowToReactFlowNodes,
} from "./reactFlowAdapters";

const now = "2026-05-15T00:00:00Z";

function makeNode(partial: Partial<WorkflowNode> & Pick<WorkflowNode, "id" | "node_type">): WorkflowNode {
  return {
    id: partial.id,
    workflow_id: partial.workflow_id ?? "workflow-1",
    node_type: partial.node_type,
    title: partial.title ?? partial.id,
    position_x: partial.position_x ?? 120,
    position_y: partial.position_y ?? 180,
    config_json: partial.config_json ?? {},
    status: partial.status ?? "idle",
    output_json: partial.output_json ?? null,
    failure_reason: partial.failure_reason ?? null,
    is_retryable: partial.is_retryable ?? false,
    attempt_count: partial.attempt_count ?? 0,
    retry_count: partial.retry_count ?? 0,
    non_retryable_reason: partial.non_retryable_reason ?? null,
    retry_hint: partial.retry_hint ?? null,
    last_run_at: partial.last_run_at ?? null,
    created_at: partial.created_at ?? now,
    updated_at: partial.updated_at ?? now,
  };
}

function makeEdge(partial: Partial<WorkflowEdge> & Pick<WorkflowEdge, "id">): WorkflowEdge {
  return {
    id: partial.id,
    workflow_id: partial.workflow_id ?? "workflow-1",
    source_node_id: partial.source_node_id ?? "product",
    target_node_id: partial.target_node_id ?? "copy",
    source_handle: partial.source_handle ?? null,
    target_handle: partial.target_handle ?? null,
    created_at: partial.created_at ?? now,
  };
}

function makeWorkflow(): ProductWorkflow {
  return {
    id: "workflow-1",
    product_id: "product-1",
    title: "Workflow",
    active: true,
    nodes: [
      makeNode({ id: "product", node_type: "product_context", position_x: 100.2, position_y: 200.4 }),
      makeNode({ id: "copy", node_type: "copy_generation", position_x: 420, position_y: 240 }),
    ],
    edges: [
      makeEdge({ id: "edge-default" }),
      makeEdge({
        id: "edge-custom",
        source_node_id: "copy",
        target_node_id: "product",
        source_handle: "custom-source",
        target_handle: "custom-target",
      }),
    ],
    runs: [],
    created_at: now,
    updated_at: now,
  };
}

describe("reactFlowAdapters", () => {
  it("maps workflow nodes without changing backend snake_case fields", () => {
    const workflow = makeWorkflow();
    const nodes = workflowToReactFlowNodes(workflow, {
      selectedNodeIds: ["copy"],
      positionOverrides: {
        product: { x: 160, y: 260 },
      },
    });

    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toMatchObject({
      id: "product",
      type: PRODUCTFLOW_NODE_TYPE,
      position: { x: 160, y: 260 },
      selected: false,
      className: "nopan",
    });
    expect(nodes[0].data.workflowNode.node_type).toBe("product_context");
    expect(nodes[0].data.workflowNode.position_x).toBe(100.2);
    expect(nodes[1]).toMatchObject({
      id: "copy",
      selected: true,
      position: { x: 420, y: 240 },
    });
  });

  it("uses optimistic drop positions when nodes are not actively dragging", () => {
    const workflow = makeWorkflow();
    const nodes = workflowToReactFlowNodes(workflow, {
      selectedNodeIds: ["copy"],
      positionOverrides: {
        copy: { x: 520, y: 340 },
      },
    });

    expect(nodes[1]).toMatchObject({
      id: "copy",
      selected: true,
      position: { x: 520, y: 340 },
    });
  });

  it("preserves ReactFlow internal drag positions over older optimistic drops while syncing node data", () => {
    const workflow = makeWorkflow();
    const nodes = workflowToReactFlowNodes(workflow, {
      selectedNodeIds: ["product", "copy"],
      previousNodes: [
        { id: "product", position: { x: 180.25, y: 260.75 } },
        { id: "copy", position: { x: 500, y: 320 } },
      ],
      preservePreviousPositionsForNodeIds: ["product", "copy"],
      positionOverrides: {
        copy: { x: 520, y: 340 },
      },
    });

    expect(nodes[0]).toMatchObject({
      id: "product",
      selected: true,
      position: { x: 180.25, y: 260.75 },
    });
    expect(nodes[1]).toMatchObject({
      id: "copy",
      selected: true,
      position: { x: 500, y: 320 },
    });
    expect(nodes[0].data.workflowNode.position_x).toBe(100.2);
  });

  it("maps workflow edges with stable handle fallbacks", () => {
    const edges = workflowToReactFlowEdges(makeWorkflow());

    expect(edges[0]).toMatchObject({
      id: "edge-default",
      type: PRODUCTFLOW_EDGE_TYPE,
      source: "product",
      target: "copy",
      sourceHandle: PRODUCTFLOW_SOURCE_HANDLE,
      targetHandle: PRODUCTFLOW_TARGET_HANDLE,
    });
    expect(edges[1]).toMatchObject({
      id: "edge-custom",
      source: "copy",
      target: "product",
      sourceHandle: "custom-source",
      targetHandle: "custom-target",
    });
    expect(edges[1]?.data?.workflowEdge.source_node_id).toBe("copy");
  });

  it("converts ReactFlow positions to integer workflow patches without coordinate clamps", () => {
    expect(reactFlowPositionToWorkflowPatch({ x: 42.6, y: 99.2 })).toEqual({
      position_x: 43,
      position_y: 99,
    });
    expect(reactFlowPositionToWorkflowPatch({ x: -20, y: 12 })).toEqual({
      position_x: -20,
      position_y: 12,
    });
  });

  it("filters unchanged position candidates before creating a drag mutation group", () => {
    const unchanged = makeNode({ id: "unchanged", node_type: "copy_generation", position_x: 100, position_y: 200 });
    const changed = makeNode({ id: "changed", node_type: "copy_generation", position_x: 420, position_y: 240 });

    expect(
      getChangedWorkflowNodePositionCandidates([
        { workflowNode: unchanged, position: { x: 100.2, y: 200.4 } },
        { workflowNode: changed, position: { x: 480.1, y: 260.2 } },
      ]),
    ).toEqual([{ workflowNode: changed, position: { x: 480.1, y: 260.2 } }]);
  });

  it("measures group node drag distance in screen pixels using the current zoom", () => {
    const startPositions = {
      product: { x: 100, y: 200 },
      copy: { x: 420, y: 240 },
    };

    expect(
      getNodeDragGroupScreenDistance(
        [
          { nodeId: "product", position: { x: 101, y: 201 } },
          { nodeId: "copy", position: { x: 422, y: 240 } },
        ],
        startPositions,
        2,
      ),
    ).toBe(4);
    expect(
      getNodeDragGroupScreenDistance([{ nodeId: "missing", position: { x: 999, y: 999 } }], startPositions, 1),
    ).toBe(0);
  });

  it("uses the screen-pixel drag guard to decide whether a group drop should persist", () => {
    const startPositions = {
      product: { x: 100, y: 200 },
      copy: { x: 420, y: 240 },
    };

    expect(
      shouldCommitNodeDragGroupPosition(
        [{ nodeId: "product", position: { x: 102.9, y: 200 } }],
        startPositions,
        1,
        3,
      ),
    ).toBe(false);
    expect(
      shouldCommitNodeDragGroupPosition(
        [{ nodeId: "product", position: { x: 103, y: 200 } }],
        startPositions,
        1,
        3,
      ),
    ).toBe(true);
    expect(
      shouldCommitNodeDragGroupPosition(
        [
          { nodeId: "product", position: { x: 101, y: 200 } },
          { nodeId: "copy", position: { x: 423, y: 244 } },
        ],
        startPositions,
        0.5,
        3,
      ),
    ).toBe(false);
  });

  it("builds workflow edge input from a ReactFlow connection without local edge materialization", () => {
    expect(
      connectionToWorkflowEdgeInput({
        source: "source",
        target: "target",
        sourceHandle: null,
        targetHandle: null,
      }),
    ).toEqual({
      source_node_id: "source",
      target_node_id: "target",
      source_handle: PRODUCTFLOW_SOURCE_HANDLE,
      target_handle: PRODUCTFLOW_TARGET_HANDLE,
    });
    expect(
      connectionToWorkflowEdgeInput({
        source: "source",
        target: "target",
        sourceHandle: "a",
        targetHandle: "b",
      }),
    ).toMatchObject({ source_handle: "a", target_handle: "b" });
    expect(
      connectionToWorkflowEdgeInput({
        source: "same",
        target: "same",
        sourceHandle: null,
        targetHandle: null,
      }),
    ).toBeNull();
    expect(
      connectionToWorkflowEdgeInput({
        source: "",
        target: "target",
        sourceHandle: null,
        targetHandle: null,
      }),
    ).toBeNull();
  });

  it("keeps ids and viewport-center node positioning explicit", () => {
    expect(workflowNodeIdFromReactFlowNode({ id: "node-1" })).toBe("node-1");
    expect(workflowNodeIdFromReactFlowNode("node-2")).toBe("node-2");
    expect(workflowEdgeIdFromReactFlowEdge({ id: "edge-1" })).toBe("edge-1");
    expect(getNodePositionForViewportCenter({ x: 640, y: 360 })).toEqual({ x: 516, y: 280 });
    expect(getNodePositionForViewportCenter({ x: -40, y: 20 })).toEqual({ x: -164, y: -60 });
    expect(normalizeWorkflowZoom(0.01)).toBe(0.05);
    expect(normalizeWorkflowZoom(0.1)).toBe(0.1);
    expect(normalizeWorkflowZoom(2)).toBe(1.6);
    expect(normalizeWorkflowZoom(1.234567)).toBe(1.2346);
  });
});

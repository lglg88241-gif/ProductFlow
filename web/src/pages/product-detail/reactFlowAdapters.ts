import type { Connection, Edge, Node, XYPosition } from "@xyflow/react";

import type { ProductWorkflow, WorkflowEdge, WorkflowNode } from "../../lib/types";
import { MAX_ZOOM, MIN_ZOOM, NODE_WIDTH } from "./constants";
import type { CanvasPoint } from "./types";
import { clamp } from "./utils";

export const PRODUCTFLOW_NODE_TYPE = "productflowNode";
export const PRODUCTFLOW_EDGE_TYPE = "productflowEdge";
export const PRODUCTFLOW_SOURCE_HANDLE = "output";
export const PRODUCTFLOW_TARGET_HANDLE = "input";

const ZOOM_PRECISION = 10_000;

export interface ProductFlowNodeData extends Record<string, unknown> {
  workflowNode: WorkflowNode;
}

export interface ProductFlowEdgeData extends Record<string, unknown> {
  workflowEdge: WorkflowEdge;
}

export type ProductFlowReactFlowNode = Node<ProductFlowNodeData, typeof PRODUCTFLOW_NODE_TYPE>;
export type ProductFlowReactFlowEdge = Edge<ProductFlowEdgeData, typeof PRODUCTFLOW_EDGE_TYPE>;

export interface WorkflowToReactFlowNodeOptions {
  selectedNodeIds?: string[];
  positionOverrides?: Record<string, CanvasPoint>;
  previousNodes?: Array<Pick<Node, "id" | "position">>;
  preservePreviousPositionsForNodeIds?: Iterable<string>;
}

export function normalizeWorkflowZoom(nextZoom: number): number {
  return clamp(Math.round(nextZoom * ZOOM_PRECISION) / ZOOM_PRECISION, MIN_ZOOM, MAX_ZOOM);
}

export function workflowToReactFlowNodes(
  workflow: ProductWorkflow,
  options: WorkflowToReactFlowNodeOptions = {},
): ProductFlowReactFlowNode[] {
  const selectedNodeIds = new Set(options.selectedNodeIds ?? []);
  const previousPositionByNodeId = new Map(options.previousNodes?.map((node) => [node.id, node.position]));
  const preservePreviousPositionsForNodeIds = new Set(options.preservePreviousPositionsForNodeIds ?? []);
  return workflow.nodes.map((node) => ({
    id: node.id,
    type: PRODUCTFLOW_NODE_TYPE,
    position:
      (preservePreviousPositionsForNodeIds.has(node.id) ? previousPositionByNodeId.get(node.id) : undefined) ??
      options.positionOverrides?.[node.id] ?? {
        x: node.position_x,
        y: node.position_y,
      },
    selected: selectedNodeIds.has(node.id),
    className: "nopan",
    draggable: true,
    selectable: true,
    connectable: true,
    deletable: false,
    data: {
      workflowNode: node,
    },
  }));
}

export function workflowToReactFlowEdges(workflow: ProductWorkflow): ProductFlowReactFlowEdge[] {
  return workflow.edges.map((edge) => ({
    id: edge.id,
    type: PRODUCTFLOW_EDGE_TYPE,
    source: edge.source_node_id,
    target: edge.target_node_id,
    sourceHandle: edge.source_handle ?? PRODUCTFLOW_SOURCE_HANDLE,
    targetHandle: edge.target_handle ?? PRODUCTFLOW_TARGET_HANDLE,
    reconnectable: false,
    deletable: false,
    focusable: false,
    data: {
      workflowEdge: edge,
    },
  }));
}

export function reactFlowPositionToWorkflowPatch(position: XYPosition): { position_x: number; position_y: number } {
  return {
    position_x: Math.round(position.x),
    position_y: Math.round(position.y),
  };
}

export function getChangedWorkflowNodePositionCandidates<
  T extends { workflowNode: Pick<WorkflowNode, "position_x" | "position_y">; position: XYPosition },
>(candidates: T[]): T[] {
  return candidates.filter(({ workflowNode, position }) => {
    const patch = reactFlowPositionToWorkflowPatch(position);
    return workflowNode.position_x !== patch.position_x || workflowNode.position_y !== patch.position_y;
  });
}

export function getNodeDragGroupScreenDistance(
  candidates: Array<{ nodeId: string; position: XYPosition }>,
  startPositions: Record<string, CanvasPoint>,
  viewportZoom: number,
): number {
  return candidates.reduce((maxDistance, candidate) => {
    const startPosition = startPositions[candidate.nodeId];
    if (!startPosition) {
      return maxDistance;
    }
    const flowDistance = Math.hypot(candidate.position.x - startPosition.x, candidate.position.y - startPosition.y);
    return Math.max(maxDistance, flowDistance * viewportZoom);
  }, 0);
}

export function shouldCommitNodeDragGroupPosition(
  candidates: Array<{ nodeId: string; position: XYPosition }>,
  startPositions: Record<string, CanvasPoint>,
  viewportZoom: number,
  commitScreenDistance: number,
): boolean {
  return getNodeDragGroupScreenDistance(candidates, startPositions, viewportZoom) >= commitScreenDistance;
}

export function getNodePositionForViewportCenter(center: CanvasPoint): CanvasPoint {
  return {
    x: Math.round(center.x - NODE_WIDTH / 2),
    y: Math.round(center.y - 80),
  };
}

export function workflowNodeIdFromReactFlowNode(node: Pick<Node, "id"> | string): string {
  return typeof node === "string" ? node : node.id;
}

export function workflowEdgeIdFromReactFlowEdge(edge: Pick<Edge, "id"> | string): string {
  return typeof edge === "string" ? edge : edge.id;
}

export function connectionToWorkflowEdgeInput(
  connection: Connection,
): { source_node_id: string; target_node_id: string; source_handle: string; target_handle: string } | null {
  if (!connection.source || !connection.target || connection.source === connection.target) {
    return null;
  }
  return {
    source_node_id: connection.source,
    target_node_id: connection.target,
    source_handle: connection.sourceHandle ?? PRODUCTFLOW_SOURCE_HANDLE,
    target_handle: connection.targetHandle ?? PRODUCTFLOW_TARGET_HANDLE,
  };
}

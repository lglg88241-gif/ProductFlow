import type { WorkflowNode } from "../../lib/types";

export function toggleSelectedNodeId(selectedNodeIds: string[], nodeId: string): string[] {
  if (selectedNodeIds.includes(nodeId)) {
    return selectedNodeIds.filter((selectedNodeId) => selectedNodeId !== nodeId);
  }
  return [...selectedNodeIds, nodeId];
}

export function focusSelectedNodeGroup(
  selectedNodeIds: string[],
  nodeId: string,
): { selectedNodeIds: string[]; primaryNodeId: string | null } {
  if (selectedNodeIds.includes(nodeId)) {
    return {
      selectedNodeIds,
      primaryNodeId: nodeId,
    };
  }
  return {
    selectedNodeIds: [nodeId],
    primaryNodeId: nodeId,
  };
}

export function clearSelectedNodeGroup(primaryNodeId: string | null): string[] {
  return primaryNodeId ? [primaryNodeId] : [];
}

export function deleteNodeFromSelection(
  selectedNodeIds: string[],
  deletedNodeId: string,
  fallbackPrimaryNodeId: string | null,
): { selectedNodeIds: string[]; primaryNodeId: string | null } {
  const remainingNodeIds = selectedNodeIds.filter((selectedNodeId) => selectedNodeId !== deletedNodeId);
  const primaryNodeId = fallbackPrimaryNodeId && fallbackPrimaryNodeId !== deletedNodeId
    ? fallbackPrimaryNodeId
    : remainingNodeIds[0] ?? null;
  return {
    selectedNodeIds: clearSelectedNodeGroup(primaryNodeId),
    primaryNodeId,
  };
}

export function replaceSelectedNodeIdsFromBox(nodeIds: string[], fallbackNodeId: string | null): {
  selectedNodeIds: string[];
  primaryNodeId: string | null;
} {
  if (nodeIds.length) {
    return {
      selectedNodeIds: nodeIds,
      primaryNodeId: nodeIds[0],
    };
  }
  return {
    selectedNodeIds: fallbackNodeId ? [fallbackNodeId] : [],
    primaryNodeId: fallbackNodeId,
  };
}

export function reconcileSelectedNodeIds(
  selectedNodeIds: string[],
  nodes: Array<Pick<WorkflowNode, "id">>,
  primaryNodeId: string | null,
): { selectedNodeIds: string[]; primaryNodeId: string | null } {
  if (!nodes.length) {
    return { selectedNodeIds: [], primaryNodeId: null };
  }
  const availableNodeIds = new Set(nodes.map((node) => node.id));
  const nextSelectedNodeIds = selectedNodeIds.filter((nodeId) => availableNodeIds.has(nodeId));
  const nextPrimaryNodeId =
    primaryNodeId && availableNodeIds.has(primaryNodeId)
      ? primaryNodeId
      : nextSelectedNodeIds[0] ?? nodes[0].id;
  const normalizedSelectedNodeIds = nextSelectedNodeIds.includes(nextPrimaryNodeId)
    ? nextSelectedNodeIds
    : [nextPrimaryNodeId, ...nextSelectedNodeIds];
  return {
    selectedNodeIds: normalizedSelectedNodeIds,
    primaryNodeId: nextPrimaryNodeId,
  };
}

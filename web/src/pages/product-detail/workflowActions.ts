import type { TranslationKey } from "../../lib/i18n";
import type { WorkflowNode } from "../../lib/types";
import type { WorkflowNodeRunActionState } from "./utils";

export type WorkflowCanvasActionId = "run" | "duplicate" | "fitSelected" | "saveTemplate" | "delete";
export type WorkflowCanvasActionIcon = "run" | "duplicate" | "fitSelected" | "saveTemplate" | "delete";

export type WorkflowCanvasActionTarget =
  | { kind: "single"; nodeId: string }
  | { kind: "group"; primaryNodeId: string; nodeIds: string[] };

export interface WorkflowCanvasActionItem {
  id: WorkflowCanvasActionId;
  icon: WorkflowCanvasActionIcon;
  labelKey?: TranslationKey;
  label?: string;
  title?: string;
  destructive?: boolean;
  disabled?: boolean;
  pending?: boolean;
}

export interface WorkflowCanvasActionToolbar {
  target: WorkflowCanvasActionTarget;
  items: WorkflowCanvasActionItem[];
}

interface WorkflowCanvasActionOptions {
  primaryNode?: WorkflowNode | null;
  targetNodes?: WorkflowNode[];
  runActionState?: WorkflowNodeRunActionState | null;
  structureBusy?: boolean;
  duplicatePending?: boolean;
  templatePending?: boolean;
  deletePending?: boolean;
}

export function getWorkflowCanvasActionTargetNodeIds(target: WorkflowCanvasActionTarget): string[] {
  return target.kind === "single" ? [target.nodeId] : [...target.nodeIds];
}

export function getWorkflowCanvasActionTargetForNodeToolbar(
  nodeId: string,
  primaryNodeId: string | null,
  selectedNodeIds: string[],
): WorkflowCanvasActionTarget | null {
  if (selectedNodeIds.length > 1) {
    if (nodeId !== primaryNodeId || !selectedNodeIds.includes(nodeId)) {
      return null;
    }
    return {
      kind: "group",
      primaryNodeId: nodeId,
      nodeIds: [...selectedNodeIds],
    };
  }
  if (nodeId === primaryNodeId || selectedNodeIds.includes(nodeId)) {
    return { kind: "single", nodeId };
  }
  return null;
}

export function buildWorkflowCanvasActionItems(
  target: WorkflowCanvasActionTarget,
  options: WorkflowCanvasActionOptions = {},
): WorkflowCanvasActionItem[] {
  const nodeIds = getWorkflowCanvasActionTargetNodeIds(target);
  const primaryNode = options.primaryNode ?? null;
  const knownTargetNodes = options.targetNodes ?? (primaryNode ? [primaryNode] : []);
  const primaryIsProductContext = primaryNode?.node_type === "product_context";
  const targetContainsProductContext = knownTargetNodes.some((node) => node.node_type === "product_context");
  const targetHasReusableNodes = knownTargetNodes.length
    ? knownTargetNodes.some((node) => node.node_type !== "product_context")
    : target.kind === "group" || !primaryIsProductContext;
  const structureBusy = Boolean(options.structureBusy);
  const items: WorkflowCanvasActionItem[] = [];

  if (target.kind === "single" && !primaryIsProductContext) {
    items.push({
      id: "run",
      icon: "run",
      label: options.runActionState?.label,
      labelKey: options.runActionState?.label ? undefined : "detail.runAction.runFromNode",
      title: options.runActionState?.title,
      disabled: Boolean(options.runActionState?.disabled),
      pending: Boolean(options.runActionState?.pending),
    });
  }

  if (targetHasReusableNodes) {
    items.push({
      id: "duplicate",
      icon: "duplicate",
      labelKey: "detail.duplicate",
      disabled: structureBusy || Boolean(options.duplicatePending) || nodeIds.length === 0,
      pending: Boolean(options.duplicatePending),
    });
  }

  items.push({
    id: "fitSelected",
    icon: "fitSelected",
    labelKey: "detail.fitSelection",
    disabled: nodeIds.length === 0,
  });

  if (nodeIds.length >= 2 && !targetContainsProductContext) {
    items.push({
      id: "saveTemplate",
      icon: "saveTemplate",
      labelKey: "detail.saveTemplate",
      disabled: structureBusy || Boolean(options.templatePending),
      pending: Boolean(options.templatePending),
    });
  }

  if (
    (target.kind === "group" && !targetContainsProductContext) ||
    (target.kind === "single" && !primaryIsProductContext)
  ) {
    items.push({
      id: "delete",
      icon: "delete",
      labelKey: "detail.delete",
      destructive: true,
      disabled: structureBusy || Boolean(options.deletePending) || nodeIds.length === 0,
      pending: Boolean(options.deletePending),
    });
  }

  return items;
}

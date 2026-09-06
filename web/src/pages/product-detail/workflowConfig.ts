import type {
  CopyPayloadV2,
  ImageToolOptionKey,
  ProductDetail,
  WorkflowNode,
  WorkflowNodeType,
} from "../../lib/types";
import {
  DEFAULT_IMAGE_TOOL_ALLOWED_FIELDS,
  compactImageToolOptions,
  imageToolOptionsFromUnknown,
} from "../../lib/imageToolOptions";
import type { NodeConfigDraft } from "./types";
import { defaultTitleForNodeType } from "./nodeDisplay";
import { configString, outputText } from "./utils";

function outputStructuredPayload(node: WorkflowNode | null): CopyPayloadV2 | null {
  const payload = node?.output_json?.structured_payload;
  if (payload && typeof payload === "object" && "version" in payload && "content" in payload) {
    return payload as CopyPayloadV2;
  }
  return null;
}

export function draftFromNode(
  node: WorkflowNode | null,
  product?: ProductDetail | null,
): NodeConfigDraft {
  const copySetId = node?.output_json
    ? outputText(node.output_json, "copy_set_id")
    : null;
  const copySet = copySetId
    ? product?.copy_sets.find((item) => item.id === copySetId)
    : null;
  return {
    title: node?.title ?? "",
    productName: configString(node, "name", product?.name ?? ""),
    category: configString(node, "category", product?.category ?? ""),
    price: configString(node, "price", product?.price ?? ""),
    sourceNote: configString(node, "source_note", product?.source_note ?? ""),
    instruction: configString(node, "instruction"),
    role: configString(node, "role", "reference"),
    label: configString(node, "label"),
    tone: configString(node, "tone", "转化清晰"),
    channel: configString(node, "channel", "商品主图"),
    size: configString(node, "size", "1024x1024"),
    toolOptions: imageToolOptionsFromUnknown(node?.config_json?.tool_options),
    copyStructuredPayload: copySet?.structured_payload ?? outputStructuredPayload(node),
  };
}

export function nodeConfigFromDraft(
  node: WorkflowNode,
  draft: NodeConfigDraft,
  imageToolAllowedFields: readonly ImageToolOptionKey[] = DEFAULT_IMAGE_TOOL_ALLOWED_FIELDS,
): Record<string, unknown> {
  const base = { ...node.config_json };
  if (node.node_type === "product_context") {
    return {
      ...base,
      name: draft.productName,
      category: draft.category,
      price: draft.price,
      source_note: draft.sourceNote,
    };
  }
  if (node.node_type === "reference_image") {
    return { ...base, role: draft.role, label: draft.label };
  }
  if (node.node_type === "copy_generation") {
    return {
      ...base,
      version: 2,
      instruction: draft.instruction,
      tone: draft.tone,
      channel: draft.channel,
      purpose: configString(node, "purpose"),
      output_mode: configString(node, "output_mode", "blocks"),
    };
  }
  if (node.node_type === "image_generation") {
    const toolOptions = compactImageToolOptions(draft.toolOptions, imageToolAllowedFields);
    return {
      ...base,
      instruction: draft.instruction,
      size: draft.size,
      ...(toolOptions ? { tool_options: toolOptions } : { tool_options: null }),
    };
  }
  return base;
}

export function defaultConfigForType(type: WorkflowNodeType): Record<string, unknown> {
  if (type === "reference_image") {
    return { role: "reference", label: "" };
  }
  if (type === "copy_generation") {
    return {
      version: 2,
      instruction: "生成商品文案",
      tone: "清晰可信",
      channel: "商品图",
      output_mode: "blocks",
    };
  }
  if (type === "image_generation") {
    return {
      instruction: "描述你想生成的图片",
      size: "1024x1024",
      tool_options: null,
    };
  }
  return {};
}

export function defaultTitleForType(type: WorkflowNodeType, index: number): string {
  return defaultTitleForNodeType(type, index);
}

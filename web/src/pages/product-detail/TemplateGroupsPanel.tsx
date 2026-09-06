import {
  ChevronDown,
  ChevronRight,
  FileText,
  ImageIcon,
  ImagePlus,
  Layers3,
  Loader2,
  Maximize2,
  Pencil,
  Plus,
  Trash2,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { localizeCanvasTemplateSummary } from "../../lib/canvasTemplateLocalization";
import type { TranslationKey } from "../../lib/i18n";
import { useI18n } from "../../lib/preferences";
import type { CanvasTemplateSummary } from "../../lib/types";
import { localizedWorkflowNodeTypeLabel } from "./nodeDisplay";

const PREVIEW_METRICS = {
  viewBoxWidth: 420,
  viewBoxHeight: 214,
  paddingX: 22,
  paddingY: 22,
  nodeWidth: 76,
  nodeHeight: 50,
} as const;

const DIALOG_PREVIEW_METRICS = {
  viewBoxWidth: 720,
  viewBoxHeight: 360,
  paddingX: 48,
  paddingY: 48,
  nodeWidth: 132,
  nodeHeight: 74,
  minColumnGap: 72,
  minRowGap: 34,
  useIndexedRows: true,
} as const;

const COMPACT_PREVIEW_METRICS = {
  viewBoxWidth: 420,
  viewBoxHeight: 154,
  paddingX: 30,
  paddingY: 24,
  nodeWidth: 54,
  nodeHeight: 42,
} as const;

const TEMPLATE_CATEGORY_ORDER = [
  { key: "all", labelKey: "detail.template.all" },
  { key: "listing", labelKey: "detail.template.listing" },
  { key: "detail", labelKey: "detail.template.detail" },
  { key: "gallery", labelKey: "detail.template.gallery" },
  { key: "content", labelKey: "detail.template.content" },
  { key: "campaign", labelKey: "detail.template.campaign" },
  { key: "custom", labelKey: "detail.template.custom" },
] as const;

type TemplateCategoryKey = (typeof TEMPLATE_CATEGORY_ORDER)[number]["key"];
type TFunction = (key: TranslationKey, params?: Record<string, string | number>) => string;

interface TemplateGroupsPanelProps {
  templates: CanvasTemplateSummary[];
  isLoading: boolean;
  isError: boolean;
  structureBusy: boolean;
  applyBusy: boolean;
  applyingTemplateKey: string | null;
  onApplyTemplate: (template: CanvasTemplateSummary) => void;
  userTemplateBusy: boolean;
  onRenameUserTemplate: (template: CanvasTemplateSummary, title: string) => void;
  onArchiveUserTemplate: (template: CanvasTemplateSummary) => void;
}

function summarizeOutput(template: CanvasTemplateSummary, t: TFunction): string {
  const labels = template.output_slots.map((slot) => slot.label).filter(Boolean);
  if (!labels.length) {
    return t("detail.template.outputSlot");
  }
  return labels[0];
}

function summarizeReferenceInput(template: CanvasTemplateSummary): string | null {
  const requiredHints = template.reference_input_hints.filter((hint) => hint.required);
  const hints = requiredHints.length ? requiredHints : template.reference_input_hints;
  const labels = hints.map((hint) => hint.label).filter(Boolean);
  if (!labels.length) {
    return null;
  }
  return labels[0];
}

function externalConnectionLabels(template: CanvasTemplateSummary): string[] {
  return Array.from(new Set(template.default_external_connections.map((connection) => connection.label).filter(Boolean)));
}

function templateCategoryKey(template: CanvasTemplateSummary): TemplateCategoryKey {
  if (template.source === "user") {
    return "custom";
  }
  const stage = template.scenario.ecommerce_stage;
  if (
    stage === "listing"
    || stage === "detail"
    || stage === "gallery"
    || stage === "content"
    || stage === "campaign"
  ) {
    return stage;
  }
  return "detail";
}

function templateCategoryCounts(templates: CanvasTemplateSummary[]): Record<TemplateCategoryKey, number> {
  const counts = Object.fromEntries(TEMPLATE_CATEGORY_ORDER.map((category) => [category.key, 0])) as Record<
    TemplateCategoryKey,
    number
  >;
  counts.all = templates.length;
  for (const template of templates) {
    counts[templateCategoryKey(template)] += 1;
  }
  return counts;
}

type PreviewNode = CanvasTemplateSummary["preview_nodes"][number] & {
  x: number;
  y: number;
  centerX: number;
  centerY: number;
};

interface TemplatePreviewLayout {
  nodes: PreviewNode[];
  nodesByKey: Map<string, PreviewNode>;
}

interface TemplatePreviewMetrics {
  viewBoxWidth: number;
  viewBoxHeight: number;
  paddingX: number;
  paddingY: number;
  nodeWidth: number;
  nodeHeight: number;
  useIndexedRows?: boolean;
}

function dialogPreviewMetricsForTemplate(template: CanvasTemplateSummary): TemplatePreviewMetrics {
  const columns = Math.max(1, new Set(template.preview_nodes.map((node) => node.position_x)).size);
  const rows = Math.max(1, new Set(template.preview_nodes.map((node) => node.position_y)).size);
  return {
    ...DIALOG_PREVIEW_METRICS,
    viewBoxWidth: Math.max(
      DIALOG_PREVIEW_METRICS.viewBoxWidth,
      DIALOG_PREVIEW_METRICS.paddingX * 2
        + DIALOG_PREVIEW_METRICS.nodeWidth
        + (columns - 1) * (DIALOG_PREVIEW_METRICS.nodeWidth + DIALOG_PREVIEW_METRICS.minColumnGap),
    ),
    viewBoxHeight: Math.max(
      DIALOG_PREVIEW_METRICS.viewBoxHeight,
      DIALOG_PREVIEW_METRICS.paddingY * 2
        + DIALOG_PREVIEW_METRICS.nodeHeight
        + (rows - 1) * (DIALOG_PREVIEW_METRICS.nodeHeight + DIALOG_PREVIEW_METRICS.minRowGap),
    ),
  };
}

function buildTemplatePreviewLayout(
  template: CanvasTemplateSummary,
  metrics: TemplatePreviewMetrics = PREVIEW_METRICS,
): TemplatePreviewLayout | null {
  const nodes = template.preview_nodes;
  if (!nodes.length) {
    return null;
  }

  // Keep only column relationships in the compact preview so wide templates remain legible.
  const sortedUniqueX = Array.from(new Set(nodes.map((node) => node.position_x))).sort((a, b) => a - b);
  const sortedUniqueY = Array.from(new Set(nodes.map((node) => node.position_y))).sort((a, b) => a - b);
  const minY = Math.min(...nodes.map((node) => node.position_y));
  const maxY = Math.max(...nodes.map((node) => node.position_y));
  const availableWidth = metrics.viewBoxWidth - metrics.paddingX * 2 - metrics.nodeWidth;
  const availableHeight = metrics.viewBoxHeight - metrics.paddingY * 2 - metrics.nodeHeight;
  const columnGap = sortedUniqueX.length <= 1 ? 0 : availableWidth / (sortedUniqueX.length - 1);
  const rowGap = sortedUniqueY.length <= 1 ? 0 : availableHeight / (sortedUniqueY.length - 1);

  const layoutNodes = nodes.map((node) => {
    const columnIndex = sortedUniqueX.indexOf(node.position_x);
    const rowIndex = sortedUniqueY.indexOf(node.position_y);
    const yRatio = minY === maxY ? 0.5 : (node.position_y - minY) / (maxY - minY);
    const x = sortedUniqueX.length <= 1
      ? (metrics.viewBoxWidth - metrics.nodeWidth) / 2
      : metrics.paddingX + columnIndex * columnGap;
    const y = metrics.useIndexedRows
      ? (
          sortedUniqueY.length <= 1
            ? (metrics.viewBoxHeight - metrics.nodeHeight) / 2
            : metrics.paddingY + rowIndex * rowGap
        )
      : (
          minY === maxY
            ? (metrics.viewBoxHeight - metrics.nodeHeight) / 2
            : metrics.paddingY + yRatio * availableHeight
        );
    return {
      ...node,
      x,
      y,
      centerX: x + metrics.nodeWidth / 2,
      centerY: y + metrics.nodeHeight / 2,
    };
  });
  return {
    nodes: layoutNodes,
    nodesByKey: new Map(layoutNodes.map((node) => [node.key, node])),
  };
}

function truncatePreviewTitle(title: string, maxLength = 7): string {
  const trimmed = title.trim();
  if (trimmed.length <= maxLength) {
    return trimmed;
  }
  return `${trimmed.slice(0, maxLength - 1).trimEnd()}...`;
}

function previewNodeMeta(nodeType: CanvasTemplateSummary["preview_nodes"][number]["node_type"], t: TFunction) {
  const iconByType: Record<CanvasTemplateSummary["preview_nodes"][number]["node_type"], LucideIcon> = {
    product_context: FileText,
    reference_image: ImagePlus,
    copy_generation: FileText,
    image_generation: ImageIcon,
  };
  const statusByType: Record<CanvasTemplateSummary["preview_nodes"][number]["node_type"], string> = {
    product_context: t("detail.nodeStatus.available"),
    reference_image: t("detail.nodeStatus.available"),
    copy_generation: t("detail.nodeStatus.idle"),
    image_generation: t("detail.nodeStatus.idle"),
  };
  if (nodeType === "copy_generation") {
    return { icon: iconByType[nodeType], label: localizedWorkflowNodeTypeLabel(nodeType, t), status: statusByType[nodeType] };
  }
  if (nodeType === "image_generation") {
    return { icon: iconByType[nodeType], label: localizedWorkflowNodeTypeLabel(nodeType, t), status: statusByType[nodeType] };
  }
  if (nodeType === "product_context") {
    return { icon: iconByType[nodeType], label: localizedWorkflowNodeTypeLabel(nodeType, t), status: statusByType[nodeType] };
  }
  return { icon: iconByType[nodeType], label: localizedWorkflowNodeTypeLabel(nodeType, t), status: statusByType[nodeType] };
}

function compactPreviewNodeLabel(nodeType: CanvasTemplateSummary["preview_nodes"][number]["node_type"], t: TFunction) {
  const labelByType: Record<CanvasTemplateSummary["preview_nodes"][number]["node_type"], TranslationKey> = {
    product_context: "detail.template.compact.productContext",
    reference_image: "detail.template.compact.referenceImage",
    copy_generation: "detail.template.compact.copyGeneration",
    image_generation: "detail.template.compact.imageGeneration",
  };
  return t(labelByType[nodeType]);
}

function edgePath(source: PreviewNode, target: PreviewNode, metrics: TemplatePreviewMetrics = PREVIEW_METRICS): string {
  const sourceX = target.centerX >= source.centerX ? source.x + metrics.nodeWidth : source.x;
  const targetX = target.centerX >= source.centerX ? target.x : target.x + metrics.nodeWidth;
  const controlOffset = Math.max(20, Math.abs(targetX - sourceX) * 0.45);
  const sourceControlX = sourceX + (target.centerX >= source.centerX ? controlOffset : -controlOffset);
  const targetControlX = targetX - (target.centerX >= source.centerX ? controlOffset : -controlOffset);
  return `M ${sourceX} ${source.centerY} C ${sourceControlX} ${source.centerY}, ${targetControlX} ${target.centerY}, ${targetX} ${target.centerY}`;
}

export function TemplateGraphPreview({
  template,
  variant = "panel",
}: {
  template: CanvasTemplateSummary;
  variant?: "panel" | "dialog";
}) {
  const { locale, t } = useI18n();
  const displayTemplate = localizeCanvasTemplateSummary(template, locale);
  const metrics = variant === "dialog" ? dialogPreviewMetricsForTemplate(displayTemplate) : PREVIEW_METRICS;
  const layout = buildTemplatePreviewLayout(displayTemplate, metrics);
  if (layout === null) {
    return (
      <div className="flex h-36 items-center justify-center border-b border-dashed border-zinc-200 bg-zinc-50 text-[11px] text-zinc-400 dark:border-slate-700 dark:bg-[#0b1220] dark:text-slate-500">
        {t("detail.template.noPreview")}
      </div>
    );
  }

  const templateId = displayTemplate.key.replace(/[^a-zA-Z0-9_-]/g, "-");
  const arrowId = `template-preview-arrow-${templateId}`;
  const gridId = `template-preview-grid-${templateId}`;
  const edges = displayTemplate.preview_edges
    .map((edge) => ({
      edge,
      source: layout.nodesByKey.get(edge.source_node_key),
      target: layout.nodesByKey.get(edge.target_node_key),
    }))
    .filter(
      (item): item is {
        edge: CanvasTemplateSummary["preview_edges"][number];
        source: PreviewNode;
        target: PreviewNode;
      } => Boolean(item.source && item.target),
    );

  return (
    <div
      role="img"
      aria-label={t("detail.template.previewAria", { title: displayTemplate.title })}
      className={
        variant === "dialog"
          ? "pointer-events-none relative overflow-hidden bg-zinc-50 dark:bg-[#0b1220]"
          : "relative h-52 overflow-hidden border-b border-zinc-100 bg-zinc-50 dark:border-slate-700 dark:bg-[#0b1220]"
      }
      style={
        variant === "dialog"
          ? {
              width: `${metrics.viewBoxWidth}px`,
              height: `${metrics.viewBoxHeight}px`,
            }
          : undefined
      }
    >
      <svg
        aria-hidden="true"
        className="absolute inset-0 h-full w-full"
        viewBox={`0 0 ${metrics.viewBoxWidth} ${metrics.viewBoxHeight}`}
        preserveAspectRatio="none"
      >
        <defs>
          <pattern id={gridId} width="14" height="14" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.7" className="fill-zinc-300 dark:fill-slate-600" />
          </pattern>
          <marker
            id={arrowId}
            markerHeight="6"
            markerUnits="strokeWidth"
            markerWidth="6"
            orient="auto"
            refX="5"
            refY="3"
          >
            <path d="M 0 0 L 6 3 L 0 6 z" fill="#6366f1" />
          </marker>
        </defs>
        <rect width={metrics.viewBoxWidth} height={metrics.viewBoxHeight} className="fill-zinc-50 dark:fill-[#0b1220]" />
        <rect width={metrics.viewBoxWidth} height={metrics.viewBoxHeight} fill={`url(#${gridId})`} opacity="0.85" />
        {edges.map(({ edge, source, target }) => (
          <path
            key={`${edge.source_node_key}->${edge.target_node_key}`}
            d={edgePath(source, target, metrics)}
            fill="none"
            markerEnd={`url(#${arrowId})`}
            stroke="#6366f1"
            strokeLinecap="round"
            strokeOpacity="0.72"
            strokeWidth="1.9"
          />
        ))}
      </svg>
      {layout.nodes.map((node) => {
        const meta = previewNodeMeta(node.node_type, t);
        const Icon = meta.icon;
        return (
          <div
            key={node.key}
            aria-label={`${node.title} ${meta.label}`}
            className="absolute rounded-lg border border-slate-200 bg-white/95 p-1.5 text-left shadow-sm backdrop-blur dark:border-slate-700 dark:bg-[#151f33]/95"
            style={{
              left: `${(node.x / metrics.viewBoxWidth) * 100}%`,
              top: `${(node.y / metrics.viewBoxHeight) * 100}%`,
              width: `${(metrics.nodeWidth / metrics.viewBoxWidth) * 100}%`,
              height: `${(metrics.nodeHeight / metrics.viewBoxHeight) * 100}%`,
            }}
          >
            <span className="absolute left-[-4px] top-1/2 h-2 w-2 -translate-y-1/2 rounded-full border border-slate-300 bg-white shadow-sm dark:border-slate-500 dark:bg-[#0b1220]" />
            <span className="absolute right-[-5px] top-1/2 h-2.5 w-2.5 -translate-y-1/2 rounded-full border-2 border-indigo-600 bg-white shadow-sm dark:border-violet-400 dark:bg-[#0b1220]" />
            <div className="flex items-start gap-1.5">
              <div className="flex min-w-0 flex-1 gap-1.5">
                <span className="mt-0.5 rounded-md border border-slate-200 bg-slate-50 p-0.5 text-slate-500 dark:border-slate-700 dark:bg-[#0b1220] dark:text-slate-300">
                  <Icon size={11} strokeWidth={2} />
                </span>
                <div className="min-w-0">
                  <div className={`${variant === "dialog" ? "text-xs leading-4" : "text-[10px] leading-3"} truncate font-semibold text-zinc-900 dark:text-slate-100`}>
                    {truncatePreviewTitle(node.title, variant === "dialog" ? 18 : 7)}
                  </div>
                  <div className={`${variant === "dialog" ? "text-[9px] leading-3" : "text-[7px] leading-none"} mt-0.5 font-medium uppercase text-zinc-400 dark:text-slate-400`}>
                    {meta.label}
                  </div>
                </div>
              </div>
            </div>
            <span className={`${variant === "dialog" ? "mt-2 px-1.5 text-[9px] leading-4" : "mt-1 px-1 text-[7px] leading-3"} inline-flex rounded-full border border-zinc-200 bg-white py-0 font-medium text-zinc-500 dark:border-slate-700 dark:bg-[#0b1220] dark:text-slate-300`}>
              {meta.status}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function CompactTemplateGraphPreview({
  template,
  onOpenPreview,
}: {
  template: CanvasTemplateSummary;
  onOpenPreview: () => void;
}) {
  const { locale, t } = useI18n();
  const displayTemplate = localizeCanvasTemplateSummary(template, locale);
  const layout = buildTemplatePreviewLayout(displayTemplate, COMPACT_PREVIEW_METRICS);

  if (layout === null) {
    return (
      <button
        type="button"
        onClick={onOpenPreview}
        className="flex h-28 w-full items-center justify-center bg-zinc-50 text-[11px] text-zinc-400 transition-colors hover:bg-zinc-100 dark:bg-[#0b1220] dark:text-slate-500 dark:hover:bg-slate-900"
      >
        {t("detail.template.noPreview")}
      </button>
    );
  }

  const templateId = displayTemplate.key.replace(/[^a-zA-Z0-9_-]/g, "-");
  const arrowId = `compact-template-preview-arrow-${templateId}`;
  const gridId = `compact-template-preview-grid-${templateId}`;
  const edges = displayTemplate.preview_edges
    .map((edge) => ({
      edge,
      source: layout.nodesByKey.get(edge.source_node_key),
      target: layout.nodesByKey.get(edge.target_node_key),
    }))
    .filter(
      (item): item is {
        edge: CanvasTemplateSummary["preview_edges"][number];
        source: PreviewNode;
        target: PreviewNode;
      } => Boolean(item.source && item.target),
    );

  return (
    <button
      type="button"
      onClick={onOpenPreview}
      aria-label={t("detail.template.openPreview", { title: displayTemplate.title })}
      title={t("detail.template.openPreview", { title: displayTemplate.title })}
      className="group relative block aspect-[16/7] min-h-28 w-full overflow-hidden bg-zinc-50 text-left transition-colors hover:bg-zinc-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 dark:bg-[#0b1220] dark:hover:bg-slate-900"
    >
      <svg
        aria-hidden="true"
        className="absolute inset-0 h-full w-full"
        viewBox={`0 0 ${COMPACT_PREVIEW_METRICS.viewBoxWidth} ${COMPACT_PREVIEW_METRICS.viewBoxHeight}`}
        preserveAspectRatio="none"
      >
        <defs>
          <pattern id={gridId} width="14" height="14" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.7" className="fill-zinc-300 dark:fill-slate-600" />
          </pattern>
          <marker
            id={arrowId}
            markerHeight="6"
            markerUnits="strokeWidth"
            markerWidth="6"
            orient="auto"
            refX="5"
            refY="3"
          >
            <path d="M 0 0 L 6 3 L 0 6 z" fill="#6366f1" />
          </marker>
        </defs>
        <rect
          width={COMPACT_PREVIEW_METRICS.viewBoxWidth}
          height={COMPACT_PREVIEW_METRICS.viewBoxHeight}
          className="fill-zinc-50 dark:fill-[#0b1220]"
        />
        <rect
          width={COMPACT_PREVIEW_METRICS.viewBoxWidth}
          height={COMPACT_PREVIEW_METRICS.viewBoxHeight}
          fill={`url(#${gridId})`}
          opacity="0.85"
        />
        {edges.map(({ edge, source, target }) => (
          <path
            key={`${edge.source_node_key}->${edge.target_node_key}`}
            d={edgePath(source, target, COMPACT_PREVIEW_METRICS)}
            fill="none"
            markerEnd={`url(#${arrowId})`}
            stroke="#6366f1"
            strokeLinecap="round"
            strokeOpacity="0.7"
            strokeWidth="2"
          />
        ))}
      </svg>
      {layout.nodes.map((node) => {
        const meta = previewNodeMeta(node.node_type, t);
        const Icon = meta.icon;
        return (
          <span
            key={node.key}
            className="absolute flex flex-col items-center justify-center gap-0.5 rounded-lg border border-slate-200 bg-white/95 text-center text-[9px] font-semibold leading-none text-slate-700 shadow-sm backdrop-blur transition-transform group-hover:scale-[1.02] dark:border-slate-700 dark:bg-[#151f33]/95 dark:text-slate-100"
            style={{
              left: `${(node.x / COMPACT_PREVIEW_METRICS.viewBoxWidth) * 100}%`,
              top: `${(node.y / COMPACT_PREVIEW_METRICS.viewBoxHeight) * 100}%`,
              width: `${(COMPACT_PREVIEW_METRICS.nodeWidth / COMPACT_PREVIEW_METRICS.viewBoxWidth) * 100}%`,
              height: `${(COMPACT_PREVIEW_METRICS.nodeHeight / COMPACT_PREVIEW_METRICS.viewBoxHeight) * 100}%`,
            }}
          >
            <Icon size={13} strokeWidth={2} className="text-indigo-600 dark:text-violet-400" />
            <span className="max-w-full truncate px-0.5">{compactPreviewNodeLabel(node.node_type, t)}</span>
          </span>
        );
      })}
      <span className="absolute right-2 top-2 inline-flex h-7 w-7 items-center justify-center rounded-md border border-zinc-200 bg-white/90 text-zinc-500 shadow-sm opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 dark:border-slate-700 dark:bg-[#151f33]/90 dark:text-slate-300">
        <Maximize2 size={13} />
      </span>
    </button>
  );
}

function TemplatePreviewDialog({
  template,
  onClose,
}: {
  template: CanvasTemplateSummary;
  onClose: () => void;
}) {
  const { locale, t } = useI18n();
  const titleId = useId();
  const previewScrollRef = useRef<HTMLDivElement | null>(null);
  const previewDragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    scrollLeft: number;
    scrollTop: number;
  } | null>(null);
  const displayTemplate = localizeCanvasTemplateSummary(template, locale);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const dialog = (
    <div
      data-template-preview-dialog
      data-vaul-no-drag
      className="pointer-events-auto fixed inset-0 z-[110] flex items-center justify-center bg-slate-950/60 p-3 backdrop-blur-sm sm:p-6"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="flex max-h-[88dvh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl shadow-slate-950/25 dark:border-slate-700 dark:bg-[#0f1726] dark:shadow-black/50"
      >
        <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-4 py-3 dark:border-slate-800 sm:px-5">
          <div className="min-w-0">
            <h2 id={titleId} className="truncate text-base font-semibold text-slate-950 dark:text-white">
              {t("detail.template.previewDialogTitle", { title: displayTemplate.title })}
            </h2>
            {displayTemplate.description ? (
              <p className="mt-1 line-clamp-2 text-xs leading-5 text-slate-500 dark:text-slate-300">
                {displayTemplate.description}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="btn-secondary-spring inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
            aria-label={t("detail.preview.close")}
            title={t("detail.preview.close")}
          >
            <X size={16} />
          </button>
        </div>
        <div
          ref={previewScrollRef}
          data-vaul-no-drag
          className="min-h-0 flex-1 cursor-grab touch-pan-x touch-pan-y overflow-auto overscroll-contain active:cursor-grabbing [-webkit-overflow-scrolling:touch]"
          onPointerDown={(event) => {
            if (event.button !== 0 && event.pointerType === "mouse") {
              return;
            }
            previewDragRef.current = {
              pointerId: event.pointerId,
              startX: event.clientX,
              startY: event.clientY,
              scrollLeft: event.currentTarget.scrollLeft,
              scrollTop: event.currentTarget.scrollTop,
            };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            const drag = previewDragRef.current;
            const scroller = previewScrollRef.current;
            if (!drag || !scroller || drag.pointerId !== event.pointerId) {
              return;
            }
            const deltaX = event.clientX - drag.startX;
            const deltaY = event.clientY - drag.startY;
            scroller.scrollLeft = drag.scrollLeft - deltaX;
            scroller.scrollTop = drag.scrollTop - deltaY;
            if (Math.abs(deltaX) > 3 || Math.abs(deltaY) > 3) {
              event.preventDefault();
            }
          }}
          onPointerUp={(event) => {
            if (previewDragRef.current?.pointerId === event.pointerId) {
              previewDragRef.current = null;
            }
          }}
          onPointerCancel={(event) => {
            if (previewDragRef.current?.pointerId === event.pointerId) {
              previewDragRef.current = null;
            }
          }}
        >
          <TemplateGraphPreview template={displayTemplate} variant="dialog" />
        </div>
      </div>
    </div>
  );

  return typeof document === "undefined" ? dialog : createPortal(dialog, document.body);
}

export function TemplateGroupsPanel({
  templates,
  isLoading,
  isError,
  structureBusy,
  applyBusy,
  applyingTemplateKey,
  onApplyTemplate,
  userTemplateBusy,
  onRenameUserTemplate,
  onArchiveUserTemplate,
}: TemplateGroupsPanelProps) {
  const { locale, t } = useI18n();
  const [editingTemplateKey, setEditingTemplateKey] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [activeCategory, setActiveCategory] = useState<TemplateCategoryKey>("all");
  const [expandedTemplateKey, setExpandedTemplateKey] = useState<string | null>(templates[0]?.key ?? null);
  const [previewTemplate, setPreviewTemplate] = useState<CanvasTemplateSummary | null>(null);

  useEffect(() => {
    if (!templates.length) {
      setExpandedTemplateKey(null);
      return;
    }
    const expandedTemplateStillVisible = templates.some(
      (template) =>
        template.key === expandedTemplateKey
        && (activeCategory === "all" || templateCategoryKey(template) === activeCategory),
    );
    if (!expandedTemplateStillVisible) {
      const nextTemplate = templates.find(
        (template) => activeCategory === "all" || templateCategoryKey(template) === activeCategory,
      );
      setExpandedTemplateKey(nextTemplate?.key ?? null);
    }
  }, [activeCategory, expandedTemplateKey, templates]);

  if (isLoading) {
    return (
      <div className="flex min-h-[180px] items-center justify-center text-zinc-400 dark:text-slate-500">
        <Loader2 size={20} className="animate-spin" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-400/35 dark:bg-red-500/10 dark:text-red-200">
        {t("detail.template.loadFailed")}
      </div>
    );
  }

  if (!templates.length) {
    return (
      <div className="glass-empty-state flex min-h-[160px] flex-col items-center justify-center gap-2 p-6 text-center text-xs text-zinc-500 dark:text-slate-400">
        <Layers3 size={18} className="text-indigo-500 opacity-80 dark:text-violet-400" />
        <div>{t("detail.template.empty")}</div>
      </div>
    );
  }

  const categoryCounts = templateCategoryCounts(templates);
  const visibleTemplates = templates.filter(
    (template) => activeCategory === "all" || templateCategoryKey(template) === activeCategory,
  );

  return (
    <section className="space-y-3">
      <div className="flex gap-1 overflow-x-auto border-b border-slate-200/50 pb-2 dark:border-slate-800">
        {TEMPLATE_CATEGORY_ORDER.filter((category) => category.key === "all" || categoryCounts[category.key] > 0).map(
          (category) => {
            const active = activeCategory === category.key;
            return (
              <button
                key={category.key}
                type="button"
                onClick={() => {
                  setActiveCategory(category.key);
                  const nextTemplate = templates.find(
                    (template) => category.key === "all" || templateCategoryKey(template) === category.key,
                  );
                  setExpandedTemplateKey(nextTemplate?.key ?? null);
                }}
                className={`shrink-0 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors ${
                  active
                    ? "border-slate-900 bg-slate-900 text-white dark:border-indigo-500/50 dark:bg-indigo-500/15 dark:text-indigo-200"
                    : "border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:border-indigo-500/40 dark:hover:bg-indigo-500/10 dark:hover:text-indigo-200"
                }`}
              >
                {t(category.labelKey)}
                <span className={active ? "ml-1 text-zinc-300" : "ml-1 text-zinc-400"}>
                  {categoryCounts[category.key]}
                </span>
              </button>
            );
          },
        )}
      </div>

      {visibleTemplates.length ? null : (
        <div className="glass-empty-state flex min-h-[120px] flex-col items-center justify-center gap-2 p-6 text-center text-xs text-zinc-500 dark:text-slate-400">
          <Layers3 size={18} className="text-indigo-500 opacity-80 dark:text-violet-400" />
          <div>{t("detail.template.emptyCategory")}</div>
        </div>
      )}

      <div className="space-y-2">
      {visibleTemplates.map((template) => {
        const displayTemplate = localizeCanvasTemplateSummary(template, locale);
        const templateBusy = applyBusy && applyingTemplateKey === template.key;
        const referenceLabel = summarizeReferenceInput(displayTemplate);
        const externalLabels = externalConnectionLabels(displayTemplate);
        const isUserTemplate = template.source === "user" && Boolean(template.user_template_id);
        const editing = editingTemplateKey === template.key;
        const expanded = expandedTemplateKey === template.key;
        return (
          <article
            key={template.key}
            className="group overflow-hidden rounded-2xl shadow-sm transition-colors config-bubble hover:border-indigo-500/30 dark:hover:border-indigo-500/40"
          >
            <div className="flex items-center gap-2 px-2.5 py-2">
              <button
                type="button"
                onClick={() => setExpandedTemplateKey(expanded ? null : template.key)}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-indigo-500/10 dark:hover:text-indigo-200"
                aria-label={expanded ? t("detail.template.collapsePreview") : t("detail.template.expandPreview")}
                title={expanded ? t("detail.template.collapsePreview") : t("detail.template.expandPreview")}
              >
                {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
              </button>
              <div className="min-w-0 flex-1 space-y-1.5 text-left">
                <div className="min-w-0">
                  <h3 className="break-words text-sm font-semibold leading-5 text-zinc-950 dark:text-white">
                    {displayTemplate.title}
                  </h3>
                </div>
                <div className="flex min-w-0 flex-wrap items-center gap-1">
                  {isUserTemplate ? (
                    <span className="rounded-sm border border-zinc-200 bg-white px-1.5 py-0.5 text-[9px] font-semibold text-zinc-600 dark:border-slate-600 dark:bg-[#0b1220] dark:text-slate-300">
                      {t("detail.template.custom")}
                    </span>
                  ) : null}
                  <span className="max-w-full truncate rounded-sm border border-emerald-100 bg-emerald-50 px-1.5 py-0.5 text-[9px] font-semibold text-emerald-700 dark:border-emerald-400/35 dark:bg-emerald-500/12 dark:text-emerald-200">
                    {summarizeOutput(displayTemplate, t)}
                  </span>
                  {referenceLabel ? (
                    <span className="max-w-full truncate rounded-sm border border-zinc-200 bg-white px-1.5 py-0.5 text-[9px] font-semibold text-zinc-600 dark:border-slate-600 dark:bg-[#0b1220] dark:text-slate-300">
                      {referenceLabel}
                    </span>
                  ) : null}
                  {externalLabels.map((label) => (
                    <span
                      key={label}
                      className="max-w-full truncate rounded-sm border border-indigo-100 bg-indigo-50 px-1.5 py-0.5 text-[9px] font-semibold text-indigo-700 dark:border-indigo-500/20 dark:bg-indigo-500/10 dark:text-indigo-200"
                    >
                      {label}
                    </span>
                  ))}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1.5">
                {isUserTemplate ? (
                  <>
                    <button
                      type="button"
                      onClick={() => {
                        setEditingTemplateKey(template.key);
                        setEditingTitle(template.title);
                      }}
                      disabled={userTemplateBusy}
                      className="btn-secondary-spring inline-flex h-8 w-8 items-center justify-center rounded-md"
                      aria-label={t("detail.template.rename")}
                      title={t("detail.template.rename")}
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      type="button"
                      onClick={() => onArchiveUserTemplate(template)}
                      disabled={userTemplateBusy}
                      className="btn-danger-spring inline-flex h-8 w-8 items-center justify-center rounded-md disabled:cursor-not-allowed disabled:opacity-50"
                      aria-label={t("detail.template.delete")}
                      title={t("detail.template.delete")}
                    >
                      <Trash2 size={13} />
                    </button>
                  </>
                ) : null}
                <button
                  type="button"
                  onClick={() => onApplyTemplate(template)}
                  disabled={structureBusy || applyBusy}
                  className="btn-primary-spring inline-flex h-8 items-center rounded-xl px-3 text-xs font-semibold"
                >
                  {templateBusy ? (
                    <Loader2 size={13} className="mr-1.5 animate-spin" />
                  ) : (
                    <Plus size={13} className="mr-1.5" />
                  )}
                  {t("detail.template.add")}
                </button>
              </div>
            </div>
            {expanded ? (
              <div className="border-t border-zinc-100 dark:border-slate-700 bg-slate-500/5 dark:bg-black/25">
                <CompactTemplateGraphPreview template={displayTemplate} onOpenPreview={() => setPreviewTemplate(template)} />
              </div>
            ) : null}
            {editing ? (
              <form
                className="flex items-center gap-2 border-t border-zinc-100 px-3 py-2 dark:border-slate-700"
                onSubmit={(event) => {
                  event.preventDefault();
                  const title = editingTitle.trim();
                  if (!title) {
                    return;
                  }
                  onRenameUserTemplate(template, title);
                  setEditingTemplateKey(null);
                }}
              >
                <input
                  value={editingTitle}
                  onChange={(event) => setEditingTitle(event.target.value)}
                  className="h-8 min-w-0 flex-1 px-2 text-xs outline-none input-premium"
                  maxLength={255}
                />
                <button
                  type="button"
                  onClick={() => setEditingTemplateKey(null)}
                  className="btn-secondary-spring h-8 rounded-md px-3 text-xs font-medium"
                >
                  {t("detail.cancel")}
                </button>
                <button
                  type="submit"
                  disabled={userTemplateBusy || !editingTitle.trim()}
                  className="btn-primary-spring h-8 rounded-md px-3 text-xs font-medium"
                >
                  {t("detail.save")}
                </button>
              </form>
            ) : null}
          </article>
        );
      })}
      </div>
      {previewTemplate ? (
        <TemplatePreviewDialog template={previewTemplate} onClose={() => setPreviewTemplate(null)} />
      ) : null}
    </section>
  );
}

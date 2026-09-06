import type { TranslateFunction } from "../../lib/preferences";
import type { ImageToolOptions } from "../../lib/types";

export interface MobileGenerationSummaryInput {
  size: string;
  generationCount: number;
  selectedReferenceCount: number;
  toolOptions: ImageToolOptions;
}

export function hasEnabledImageToolOptions(toolOptions: ImageToolOptions): boolean {
  return Object.values(toolOptions).some((value) => {
    return typeof value === "string" && value.trim().length > 0;
  }) || Object.values(toolOptions).some((value) => {
    return typeof value === "number" && Number.isFinite(value);
  });
}

export function formatMobileGenerationSummary(
  input: MobileGenerationSummaryInput,
  t: TranslateFunction,
): string {
  const parts = [
    t("chat.mobileSummarySize", { size: input.size }),
    t("chat.mobileSummaryCount", { count: input.generationCount }),
  ];
  if (input.selectedReferenceCount > 0) {
    parts.push(t("chat.mobileSummaryReferences", { count: input.selectedReferenceCount }));
  }
  parts.push(t(hasEnabledImageToolOptions(input.toolOptions) ? "chat.mobileSummaryAdvancedOn" : "chat.mobileSummaryAdvancedOff"));
  return parts.join(" · ");
}

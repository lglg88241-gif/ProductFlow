import { describe, expect, it } from "vitest";

import type { TranslationKey, TranslationParams } from "../../lib/i18n";
import type { TranslateFunction } from "../../lib/preferences";
import { formatMobileGenerationSummary, hasEnabledImageToolOptions } from "./mobileLayout";

const messages: Partial<Record<TranslationKey, string>> = {
  "chat.mobileSummarySize": "Size {size}",
  "chat.mobileSummaryCount": "{count} candidates",
  "chat.mobileSummaryReferences": "{count} references",
  "chat.mobileSummaryAdvancedOn": "Advanced on",
  "chat.mobileSummaryAdvancedOff": "Advanced off",
};

const t = ((key: TranslationKey, params?: TranslationParams) => {
  let message = messages[key] ?? key;
  Object.entries(params ?? {}).forEach(([name, value]) => {
    message = message.replace(`{${name}}`, String(value));
  });
  return message;
}) as TranslateFunction;

describe("hasEnabledImageToolOptions", () => {
  it("treats non-empty strings and finite numbers as enabled options", () => {
    expect(hasEnabledImageToolOptions({ quality: "high" })).toBe(true);
    expect(hasEnabledImageToolOptions({ output_compression: 80 })).toBe(true);
  });

  it("ignores empty strings and nullish values", () => {
    expect(hasEnabledImageToolOptions({ model: " ", quality: null })).toBe(false);
  });
});

describe("formatMobileGenerationSummary", () => {
  it("formats size, count, references, and advanced state", () => {
    expect(
      formatMobileGenerationSummary(
        {
          size: "1024x1024",
          generationCount: 3,
          selectedReferenceCount: 2,
          toolOptions: { quality: "high" },
        },
        t,
      ),
    ).toBe("Size 1024x1024 · 3 candidates · 2 references · Advanced on");
  });

  it("omits the reference segment when no references are selected", () => {
    expect(
      formatMobileGenerationSummary(
        {
          size: "1536x1024",
          generationCount: 1,
          selectedReferenceCount: 0,
          toolOptions: {},
        },
        t,
      ),
    ).toBe("Size 1536x1024 · 1 candidates · Advanced off");
  });
});

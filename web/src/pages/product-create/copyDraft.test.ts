import { describe, expect, it } from "vitest";

import { appendExtractedCopy, normalizeProductPriceForApi, serializePosterSourceNote } from "./copyDraft";

describe("poster copy draft", () => {
  it("serializes every supplied fact into source_note", () => {
    const note = serializePosterSourceNote({
      projectName: "夏日轻体项目",
      scene: "轻体管理",
      draft: {
        brand: "净研所",
        subject: "轻体体验课",
        price: "399 元",
        date: "2026.08.31",
        headline: "轻盈一点，自在一点",
        subheadline: "门店体验项目",
        sellingPoints: ["一对一评估", "周期可调整", "到店体验"],
        supplement: "具体安排以门店说明为准",
        rawCopy: "原始活动文案",
      },
    });

    expect(note).toContain("项目名称: 夏日轻体项目");
    expect(note).toContain("业务场景: 轻体管理");
    expect(note).toContain("品牌: 净研所");
    expect(note).toContain("日期: 2026.08.31");
    expect(note).toContain("价格: 399 元");
    expect(note).toContain("1. 一对一评估");
    expect(note).toContain("原始文案: 原始活动文案");
    expect(note).not.toContain("二维码");
    expect(note).not.toContain("操作按钮");
  });

  it("appends recognized copy without discarding existing edits", () => {
    expect(appendExtractedCopy("已写内容", "  新识别内容\n")).toBe("已写内容\n\n新识别内容");
    expect(appendExtractedCopy("", "  新识别内容\n")).toBe("新识别内容");
  });

  it("normalizes natural-language prices for the numeric product field", () => {
    expect(normalizeProductPriceForApi("体验价 399 元")).toBe("399");
    expect(normalizeProductPriceForApi("原价 699 / 体验价 399")).toBe("399");
    expect(normalizeProductPriceForApi("¥1,299.00 起")).toBe("1299.00");
    expect(normalizeProductPriceForApi("到店确认")).toBeUndefined();
  });
});

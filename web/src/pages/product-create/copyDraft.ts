export interface PosterCopyDraft {
  brand: string;
  subject: string;
  price: string;
  date: string;
  headline: string;
  subheadline: string;
  sellingPoints: string[];
  supplement: string;
  rawCopy: string;
}

export const EMPTY_POSTER_COPY_DRAFT: PosterCopyDraft = {
  brand: "",
  subject: "",
  price: "",
  date: "",
  headline: "",
  subheadline: "",
  sellingPoints: ["", "", "", ""],
  supplement: "",
  rawCopy: "",
};

interface PosterCopySource {
  projectName?: string;
  scene?: string;
  draft: PosterCopyDraft;
}

export function serializePosterSourceNote({ projectName, scene, draft }: PosterCopySource): string {
  const sections: Array<[string, string | string[] | undefined]> = [
    ["项目名称", projectName],
    ["业务场景", scene],
    ["品牌", draft.brand],
    ["产品/活动", draft.subject],
    ["价格", draft.price],
    ["日期", draft.date],
    ["主标题", draft.headline],
    ["副标题", draft.subheadline],
    ["卖点", draft.sellingPoints],
    ["补充说明", draft.supplement],
    ["原始文案", draft.rawCopy],
  ];

  return sections
    .flatMap(([label, value]) => {
      if (Array.isArray(value)) {
        const points = value.map((item) => item.trim()).filter(Boolean);
        return points.length ? [`${label}:\n${points.map((item, index) => `${index + 1}. ${item}`).join("\n")}`] : [];
      }
      const normalized = value?.trim();
      return normalized ? [`${label}: ${normalized}`] : [];
    })
    .join("\n");
}

export function appendExtractedCopy(current: string, extracted: string): string {
  const next = extracted.trim();
  if (!next) {
    return current;
  }
  return current.trim() ? `${current.trim()}\n\n${next}` : next;
}

export function normalizeProductPriceForApi(value: string): string | undefined {
  const matches = value.replace(/,/g, "").match(/\d+(?:\.\d{1,2})?/g);
  return matches?.at(-1);
}

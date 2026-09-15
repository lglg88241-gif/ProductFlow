import { describe, expect, it } from "vitest";

import {
  DEFAULT_LOCALE,
  LOCALES,
  LOCALE_LABEL_KEYS,
  ensureLocale,
  getDictionaryVersion,
  getLoadedDictionary,
  interpolate,
  isLocaleLoaded,
  resolveLocale,
  subscribeToDictionaries,
  translate,
} from "./i18n";
import { resolveTheme, resolveThemePreference } from "./theme";

describe("i18n helpers", () => {
  it("falls back to zh-CN copy while a locale dictionary is not loaded, then refreshes after load", async () => {
    // 模块注册表此时只有 zh-CN 同步加载（本文件尚未触发任何 ensureLocale）
    expect(isLocaleLoaded("ja-JP")).toBe(false);
    expect(getLoadedDictionary("ja-JP")).toBeUndefined();
    expect(translate("ja-JP", "nav.language")).toBe(translate("zh-CN", "nav.language"));

    const versions: number[] = [];
    const unsubscribe = subscribeToDictionaries(() => versions.push(getDictionaryVersion()));
    try {
      await ensureLocale("ja-JP");
    } finally {
      unsubscribe();
    }

    expect(isLocaleLoaded("ja-JP")).toBe(true);
    expect(translate("ja-JP", "nav.language")).toBe("言語");
    expect(versions.length).toBeGreaterThan(0); // 字典注册后通知订阅者（触发全局重渲染）
    await expect(ensureLocale("ja-JP")).resolves.toBeUndefined(); // 重复加载命中内存缓存
  });

  it("resolves supported locales and falls back to Chinese", () => {
    expect(resolveLocale("en-US")).toBe("en-US");
    expect(resolveLocale("zh-CN")).toBe("zh-CN");
    expect(resolveLocale("vi-VN")).toBe("vi-VN");
    expect(resolveLocale("fr-FR")).toBe(DEFAULT_LOCALE);
    expect(resolveLocale(null)).toBe(DEFAULT_LOCALE);
  });

  it("keeps locale selector metadata aligned with supported locales", async () => {
    await Promise.all(LOCALES.map((locale) => ensureLocale(locale)));
    const defaultKeys = Object.keys(getLoadedDictionary(DEFAULT_LOCALE) ?? {}).sort();

    expect(Object.keys(LOCALE_LABEL_KEYS).sort()).toEqual([...LOCALES].sort());
    for (const locale of LOCALES) {
      expect(Object.keys(getLoadedDictionary(locale) ?? {}).sort()).toEqual(defaultKeys);
    }
    expect(translate("vi-VN", "locale.viVN")).toBe("Tiếng Việt");
    expect(translate("vi-VN", "nav.language")).toBe("Ngôn ngữ");
    expect(translate("vi-VN", "detail.runWorkflow")).toBe(
      "Lưu cấu hình hiện tại rồi chạy toàn bộ quy trình trên canvas",
    );
  });

  it("translates keys with interpolation", () => {
    expect(translate("zh-CN", "products.paginationSummary", { page: 2, totalPages: 5, total: 48 })).toBe(
      "第 2 / 5 页 · 共 48 个项目",
    );
    expect(translate("en-US", "products.paginationSummary", { page: 2, totalPages: 5, total: 48 })).toBe(
      "Page 2 / 5 · 48 projects",
    );
    expect(interpolate("Hello {name}, {missing}", { name: "Ada" })).toBe("Hello Ada, {missing}");
  });
});

describe("theme helpers", () => {
  it("resolves persisted theme preference values", () => {
    expect(resolveThemePreference("dark")).toBe("dark");
    expect(resolveThemePreference("light")).toBe("light");
    expect(resolveThemePreference("system")).toBe("system");
    expect(resolveThemePreference("sepia")).toBe("system");
  });

  it("resolves system mode from prefers-color-scheme", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
  });
});

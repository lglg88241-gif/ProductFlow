import { zhCN } from "./i18n/zh-CN";

export const LOCALES = ["zh-CN", "en-US", "ja-JP", "vi-VN"] as const;

export type Locale = (typeof LOCALES)[number];
export type TranslationParams = Record<string, string | number>;

export const DEFAULT_LOCALE: Locale = "zh-CN";
export const LOCALE_STORAGE_KEY = "productflow.locale";

export { zhCN };

export type TranslationKey = keyof typeof zhCN;
export type Dictionary = Record<TranslationKey, string>;

/**
 * 已注册（同步可查）的字典表：zh-CN 随主包同步加载，en/ja/vi 通过 ensureLocale 动态 import 懒加载。
 * 未加载语言在 translate 里回落 zh-CN 文案，加载完成后经字典版本号触发全局重渲染。
 */
const loadedDictionaries: Record<Locale, Dictionary | undefined> = {
  "zh-CN": zhCN,
  "en-US": undefined,
  "ja-JP": undefined,
  "vi-VN": undefined,
};

const dictionaryLoaders: Record<Locale, () => Promise<Dictionary>> = {
  "zh-CN": () => Promise.resolve(zhCN),
  "en-US": () => import("./i18n/en-US").then((module) => module.enUS),
  "ja-JP": () => import("./i18n/ja-JP").then((module) => module.jaJP),
  "vi-VN": () => import("./i18n/vi-VN").then((module) => module.viVN),
};

const pendingLoads = new Map<Locale, Promise<void>>();
const dictionaryListeners = new Set<() => void>();
let dictionaryVersion = 0;

function registerDictionary(locale: Locale, dictionary: Dictionary): void {
  loadedDictionaries[locale] = dictionary;
  dictionaryVersion += 1;
  for (const listener of dictionaryListeners) listener();
}

/** 当前字典版本号：字典注册完成后自增，供 useSyncExternalStore 触发全局重渲染。 */
export function getDictionaryVersion(): number {
  return dictionaryVersion;
}

/** 订阅字典注册事件（懒加载完成时通知），返回退订函数。 */
export function subscribeToDictionaries(listener: () => void): () => void {
  dictionaryListeners.add(listener);
  return () => {
    dictionaryListeners.delete(listener);
  };
}

/** 语言字典是否已同步可用。 */
export function isLocaleLoaded(locale: Locale): boolean {
  return loadedDictionaries[locale] !== undefined;
}

/** 已注册语言的字典（未加载返回 undefined），供测试与调试读取。 */
export function getLoadedDictionary(locale: Locale): Dictionary | undefined {
  return loadedDictionaries[locale];
}

/** 懒加载指定语言字典并内存缓存；zh-CN 同步可用立即返回，加载失败清理挂起记录以便重试。 */
export function ensureLocale(locale: Locale): Promise<void> {
  if (loadedDictionaries[locale] !== undefined) return Promise.resolve();
  const pending = pendingLoads.get(locale);
  if (pending) return pending;
  const load = dictionaryLoaders[locale]()
    .then((dictionary) => {
      registerDictionary(locale, dictionary);
    })
    .catch((error: unknown) => {
      pendingLoads.delete(locale);
      throw error;
    });
  pendingLoads.set(locale, load);
  return load;
}

export const LOCALE_LABEL_KEYS = {
  "zh-CN": "locale.zhCN",
  "en-US": "locale.enUS",
  "ja-JP": "locale.jaJP",
  "vi-VN": "locale.viVN",
} satisfies Record<Locale, TranslationKey>;

export function isLocale(value: string | null | undefined): value is Locale {
  return LOCALES.includes(value as Locale);
}

export function resolveLocale(value: string | null | undefined): Locale {
  return isLocale(value) ? value : DEFAULT_LOCALE;
}

export function interpolate(template: string, params: TranslationParams = {}): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) => {
    const value = params[key];
    return value === undefined ? match : String(value);
  });
}

/** 同步翻译：未加载语言先回落 zh-CN 文案，字典加载完成后由订阅机制触发重渲染更新。 */
export function translate(locale: Locale, key: TranslationKey, params?: TranslationParams): string {
  const dictionary = loadedDictionaries[locale] ?? loadedDictionaries[DEFAULT_LOCALE] ?? zhCN;
  return interpolate(dictionary[key] ?? zhCN[key], params);
}

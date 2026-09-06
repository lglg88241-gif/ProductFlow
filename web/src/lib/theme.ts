export const THEME_PREFERENCES = ["light", "dark", "system"] as const;

export type ThemePreference = (typeof THEME_PREFERENCES)[number];
export type ResolvedTheme = "light" | "dark";

export const DEFAULT_THEME_PREFERENCE: ThemePreference = "system";
export const THEME_STORAGE_KEY = "productflow.theme";

export function isThemePreference(value: string | null | undefined): value is ThemePreference {
  return THEME_PREFERENCES.includes(value as ThemePreference);
}

export function resolveThemePreference(value: string | null | undefined): ThemePreference {
  return isThemePreference(value) ? value : DEFAULT_THEME_PREFERENCE;
}

export function resolveTheme(preference: ThemePreference, systemPrefersDark: boolean): ResolvedTheme {
  if (preference === "system") {
    return systemPrefersDark ? "dark" : "light";
  }
  return preference;
}

export function applyThemeToRoot(root: HTMLElement, resolvedTheme: ResolvedTheme, preference: ThemePreference): void {
  root.classList.toggle("dark", resolvedTheme === "dark");
  root.dataset.theme = resolvedTheme;
  root.dataset.themePreference = preference;
}


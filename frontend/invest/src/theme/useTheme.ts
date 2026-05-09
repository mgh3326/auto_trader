import { useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "invest-theme";
const DEFAULT_THEME: Theme = "dark";

export function applyTheme(t: Theme): void {
  const html = document.documentElement;
  if (t === "system") {
    html.removeAttribute("data-theme");
  } else {
    html.dataset.theme = t;
  }
}

export function readStoredTheme(): Theme {
  if (typeof localStorage === "undefined") return DEFAULT_THEME;
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw === "light" || raw === "dark" || raw === "system" ? raw : DEFAULT_THEME;
}

// Apply the stored (or default) theme synchronously, before React renders,
// so the first paint already matches and there is no light/dark flash.
export function applyInitialTheme(): void {
  applyTheme(readStoredTheme());
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme());
  useEffect(() => {
    applyTheme(theme);
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(STORAGE_KEY, theme);
    }
  }, [theme]);
  return [theme, setTheme] as const;
}

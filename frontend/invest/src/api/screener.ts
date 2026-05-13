import type {
  ScreenerPresetsResponse,
  ScreenerMarket,
  ScreenerResultsResponse,
} from "../types/screener";

export async function fetchScreenerPresets(
  market: ScreenerMarket = "kr",
): Promise<ScreenerPresetsResponse> {
  const q = new URLSearchParams({ market });
  const res = await fetch(`/invest/api/screener/presets?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`screener/presets ${res.status}`);
  return res.json();
}

export async function fetchScreenerResults(
  presetId: string,
  market: ScreenerMarket = "kr",
): Promise<ScreenerResultsResponse> {
  const q = new URLSearchParams({ preset: presetId, market });
  const res = await fetch(`/invest/api/screener/results?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`screener/results ${res.status}`);
  return res.json();
}

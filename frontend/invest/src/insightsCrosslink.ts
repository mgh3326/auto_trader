// Shared crosslink key helpers for /insights (ROB-682).
//
// ROB-678 wired the forecast↔retrospective crosslink on exact `correlation_id`
// equality, but the two id namespaces are disjoint in practice (forecast side
// is thesis-style free text; retro side is exec-style `toss_live:...` /
// `live:<uuid>`), so the intersection was always empty and the anchors/links
// never rendered. This module re-keys the crosslink on a normalized
// market-qualified SYMBOL instead — the one thing both sides genuinely share.
//
// Both ForecastCalibrationPanel and RetrospectivesPanel MUST derive their keys
// through this module only. If the two panels compute keys differently, the
// crosslink goes dead again in the same way ROB-678's correlation_id scheme
// did.
//
// Normalization mirrors the server side so the same instrument folds to the
// same key on both axes:
//   - equity (kr/us): `upper()` + separators `(-|/) → .`, matching
//     `app/core/symbol.to_db_symbol` / `trade_retrospective_service._normalize_symbol`.
//   - crypto: reuse `stockDetailRouteSymbol("crypto", …)` (→ `KRW-<COIN>`),
//     which already folds dot-format crypto symbols (ROB-683).
import { stockDetailRouteSymbol } from "./stockDetailPath";

export type CrosslinkMarket = "kr" | "us" | "crypto";

// forecast_row.instrument_type ("equity_kr" | "equity_us" | "crypto") → market.
// Single definition for crosslink purposes — panels may keep their own local
// copies for display concerns (money formatting, hrefs), but crosslink key
// derivation must go through forecastMarket()/retroMarket() below.
const INSTRUMENT_MARKET: Record<string, CrosslinkMarket> = {
  equity_kr: "kr",
  equity_us: "us",
  crypto: "crypto",
};

function asCrosslinkMarket(value: string | null | undefined): CrosslinkMarket | null {
  return value === "kr" || value === "us" || value === "crypto" ? value : null;
}

export function forecastMarket(instrumentType: string | null): CrosslinkMarket | null {
  if (!instrumentType) return null;
  return INSTRUMENT_MARKET[instrumentType] ?? null;
}

// Retrospective rows carry an explicit `market` column; fall back to
// `instrument_type` only when market is missing (mirrors server precedence).
export function retroMarket(
  market: string | null,
  instrumentType: string | null,
): CrosslinkMarket | null {
  return asCrosslinkMarket(market) ?? forecastMarket(instrumentType);
}

// Canonical crosslink key. Either side missing (market null/unknown, or an
// empty symbol) drops the link entirely rather than risk a cross-market or
// cross-instrument false match.
export function crosslinkKey(market: CrosslinkMarket | null, symbol: string): string | null {
  const trimmed = symbol.trim();
  if (!market || !trimmed) return null;
  if (market === "crypto") return `crypto:${stockDetailRouteSymbol("crypto", trimmed)}`;
  return `${market}:${trimmed.toUpperCase().replace(/[-/]/g, ".")}`;
}

// URL-fragment / DOM-id safe slug for a crosslink key (anchors + hrefs).
// Non-alphanumeric characters (":", ".", "-") all fold to "-".
export function crosslinkAnchorSlug(key: string): string {
  return key.replace(/[^a-zA-Z0-9]/g, "-");
}

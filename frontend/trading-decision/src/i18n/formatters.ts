import { COMMON } from "./ko";

export function labelOrToken<K extends string>(
  map: Readonly<Record<K, string>>,
  key: string | null | undefined,
): string {
  if (key === null || key === undefined || key === "") return COMMON.dash;
  const known = (map as Record<string, string>)[key];
  if (known !== undefined) return known;
  return formatToken(key);
}

export function labelOperatorToken(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return COMMON.dash;
  return formatToken(value);
}

export function labelOrderSide(side: string | null | undefined): string {
  if (side === "buy") return "매수";
  if (side === "sell") return "매도";
  return COMMON.dash;
}

export function labelYesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return COMMON.dash;
  return value ? COMMON.yes : COMMON.no;
}

function formatToken(raw: string): string {
  return raw.replace(/_/g, " ");
}

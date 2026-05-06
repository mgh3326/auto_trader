// frontend/invest/src/components/discover/impactMap.ts
import type { NewsRadarRiskCategory } from "../../types/newsRadar";

export type ImpactTone = "positive" | "negative" | "watch";

export interface ImpactPill {
  theme: string;
  tone: ImpactTone;
  note: string;
}

export const IMPACT_MAP: Record<NewsRadarRiskCategory, ImpactPill[]> = {
  geopolitical_oil: [
    { theme: "원유/에너지", tone: "watch", note: "변동성/수혜 가능" },
    { theme: "항공/운송", tone: "negative", note: "비용 압박 가능" },
    { theme: "금/방산", tone: "positive", note: "방어적 선호 가능" },
  ],
  macro_policy: [
    { theme: "금리 민감 성장주", tone: "negative", note: "부담 가능" },
    { theme: "금융", tone: "watch", note: "금리/스프레드 영향" },
  ],
  earnings_bigtech: [
    { theme: "AI/반도체", tone: "watch", note: "수요/실적 민감" },
    { theme: "나스닥", tone: "watch", note: "투자심리 영향" },
  ],
  crypto_security: [
    { theme: "가상자산", tone: "negative", note: "보안/규제 리스크" },
  ],
  korea_market: [
    { theme: "국내 증시", tone: "watch", note: "수급/정책/환율 영향" },
  ],
};

export function lookupImpact(category: NewsRadarRiskCategory | null): ImpactPill[] | null {
  if (!category) return null;
  return IMPACT_MAP[category] ?? null;
}

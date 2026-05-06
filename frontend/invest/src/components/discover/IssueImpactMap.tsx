// frontend/invest/src/components/discover/IssueImpactMap.tsx
import type { CSSProperties } from "react";
import type { NewsRadarRiskCategory } from "../../types/newsRadar";
import { lookupImpact, type ImpactPill, type ImpactTone } from "./impactMap";

const TONE_STYLES: Record<ImpactTone, CSSProperties> = {
  positive: { background: "var(--pill-mix)", color: "var(--pill-mix-fg)" },
  negative: { background: "var(--pill-toss)", color: "var(--pill-toss-fg)" },
  watch: { background: "var(--pill-up)", color: "var(--pill-up-fg)" },
};

type IssueImpactMapProps = Readonly<{
  category: NewsRadarRiskCategory | null;
}>;

type ImpactRowProps = Readonly<{
  pill: ImpactPill;
}>;

export function IssueImpactMap({ category }: IssueImpactMapProps) {
  const pills = lookupImpact(category);
  return (
    <section aria-labelledby="impact-heading" style={{ marginTop: 16 }}>
      <h2 id="impact-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
        어떤 영향을 줄까?
      </h2>
      {pills && pills.length > 0 ? (
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: "8px 0 0",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          {pills.map((pill) => (
            <ImpactRow key={pill.theme} pill={pill} />
          ))}
        </ul>
      ) : (
        <div className="subtle" style={{ marginTop: 8 }}>
          이 이슈에 대한 영향 분석은 준비 중입니다.
        </div>
      )}
      <div className="subtle" style={{ marginTop: 12, fontSize: 11 }}>
        뉴스 기반 참고 정보이며 매매 추천이 아닙니다.
      </div>
    </section>
  );
}

function ImpactRow({ pill }: ImpactRowProps) {
  return (
    <li
      style={{
        display: "flex",
        gap: 8,
        alignItems: "center",
        padding: "8px 12px",
        borderRadius: 999,
        ...TONE_STYLES[pill.tone],
        fontSize: 12,
      }}
    >
      <strong>{pill.theme}</strong>
      <span style={{ opacity: 0.85 }}>{pill.note}</span>
    </li>
  );
}

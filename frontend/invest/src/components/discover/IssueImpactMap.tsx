// frontend/invest/src/components/discover/IssueImpactMap.tsx
import type { CSSProperties } from "react";
import type { IssueDirection } from "../../types/newsIssues";

const TONE_STYLES: Record<IssueDirection, CSSProperties> = {
  up: { background: "var(--pill-mix)", color: "var(--pill-mix-fg)" },
  down: { background: "var(--pill-toss)", color: "var(--pill-toss-fg)" },
  mixed: { background: "var(--pill-up)", color: "var(--pill-up-fg)" },
  neutral: { background: "var(--surface-2)", color: "var(--muted)" },
};

const DIRECTION_COPY: Record<IssueDirection, string> = {
  up: "관련 종목·섹터에 긍정 모멘텀으로 해석될 수 있어요.",
  down: "관련 종목·섹터에 부담 요인으로 해석될 수 있어요.",
  mixed: "수혜와 부담이 함께 나타날 수 있어 가격 반응을 나눠 봐야 해요.",
  neutral: "방향성은 아직 뚜렷하지 않아 추가 뉴스 확인이 필요해요.",
};

type IssueImpactMapProps = Readonly<{
  direction: IssueDirection;
  sectors: readonly string[];
}>;

export function IssueImpactMap({ direction, sectors }: IssueImpactMapProps) {
  const labels = sectors.length > 0 ? sectors : ["관련 시장"];
  return (
    <section aria-labelledby="impact-heading" style={{ marginTop: 16 }}>
      <h2 id="impact-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
        어떤 영향을 줄까?
      </h2>
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
        {labels.map((label) => (
          <li
            key={label}
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              padding: "8px 12px",
              borderRadius: 999,
              ...TONE_STYLES[direction],
              fontSize: 12,
            }}
          >
            <strong>{label}</strong>
            <span style={{ opacity: 0.85 }}>{DIRECTION_COPY[direction]}</span>
          </li>
        ))}
      </ul>
      <div className="subtle" style={{ marginTop: 12, fontSize: 11 }}>
        뉴스 기반 참고 정보이며 매매 추천이 아닙니다.
      </div>
    </section>
  );
}

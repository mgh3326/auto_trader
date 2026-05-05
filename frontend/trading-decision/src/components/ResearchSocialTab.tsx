import type { SocialSignals, StageAnalysis } from "../api/types";

interface Props {
  stage: StageAnalysis | null;
}

export default function ResearchSocialTab({ stage }: Props) {
  const s = stage?.signals as SocialSignals | undefined;
  if (!stage || !s || s.available === false) {
    return (
      <div role="status" aria-label="소셜 단계 준비 중">
        <p>
          🔧 소셜 신호 분석은 준비 중입니다. Reddit · X 데이터 수집 인프라 구축
          후 활성화됩니다.
        </p>
      </div>
    );
  }
  return (
    <div>
      <p>소셜 단계 결과: {stage.verdict}</p>
    </div>
  );
}

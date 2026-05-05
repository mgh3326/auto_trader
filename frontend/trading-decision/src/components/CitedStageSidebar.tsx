import {
  LINK_DIRECTION_LABEL,
  STAGE_TYPE_LABEL,
} from "../i18n/ko";
import type { StageAnalysis, SummaryStageLink } from "../api/types";

interface Props {
  links: SummaryStageLink[];
  stages: StageAnalysis[];
  onJumpToStage: (stageType: string) => void;
}

export default function CitedStageSidebar({
  links,
  stages,
  onJumpToStage,
}: Props) {
  if (links.length === 0) {
    return (
      <aside aria-label="인용된 단계">
        <p>인용된 단계가 없습니다.</p>
      </aside>
    );
  }

  const stageById = new Map(stages.map((s) => [s.id, s]));
  const grouped: Record<string, SummaryStageLink[]> = {
    support: [],
    contradict: [],
    context: [],
  };
  for (const link of links) {
    grouped[link.direction]?.push(link);
  }

  return (
    <aside aria-label="인용된 단계">
      <h3>인용된 단계</h3>
      {(["support", "contradict", "context"] as const).map((dir) => {
        const items = grouped[dir];
        if (!items || items.length === 0) return null;
        return (
          <section key={dir}>
            <h4>{LINK_DIRECTION_LABEL[dir]}</h4>
            <ul>
              {items.map((link) => {
                const stage = stageById.get(link.stage_analysis_id);
                const isUnavailable =
                  stage?.verdict === "unavailable" ||
                  stage?.stage_type === "social";
                return (
                  <li key={link.stage_analysis_id}>
                    <button
                      type="button"
                      onClick={() => onJumpToStage(link.stage_type)}
                      aria-label={`${STAGE_TYPE_LABEL[link.stage_type]} 단계로 이동`}
                      data-unavailable={isUnavailable ? "true" : undefined}
                    >
                      {STAGE_TYPE_LABEL[link.stage_type]}
                      {link.rationale ? `: ${link.rationale}` : ""}
                      {" "}({Math.round(link.weight * 100)}%)
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </aside>
  );
}

import { SUMMARY_DECISION_LABEL } from "../i18n/ko";
import type { ResearchSessionFullResponse } from "../api/types";
import CitedStageSidebar from "./CitedStageSidebar";

interface Props {
  data: ResearchSessionFullResponse;
  onJumpToStage: (stageType: string) => void;
}

export default function ResearchSummaryTab({ data, onJumpToStage }: Props) {
  const summary = data.summary;
  if (!summary) {
    return <p>요약이 아직 준비되지 않았습니다.</p>;
  }

  const price = summary.price_analysis;

  return (
    <div>
      <header>
        <span data-decision={summary.decision}>
          {SUMMARY_DECISION_LABEL[summary.decision]}
        </span>
        <progress
          value={summary.confidence}
          max={100}
          aria-label="신뢰도"
        >
          {summary.confidence}%
        </progress>
      </header>

      {price ? (
        <section aria-label="가격 분석">
          <h3>가격 분석</h3>
          <dl>
            {price.appropriate_buy_min != null && (
              <>
                <dt>적정 매수 범위</dt>
                <dd>
                  {price.appropriate_buy_min} ~ {price.appropriate_buy_max}
                </dd>
              </>
            )}
            {price.appropriate_sell_min != null && (
              <>
                <dt>적정 매도 범위</dt>
                <dd>
                  {price.appropriate_sell_min} ~ {price.appropriate_sell_max}
                </dd>
              </>
            )}
            {price.buy_hope_min != null && (
              <>
                <dt>희망 매수 범위</dt>
                <dd>
                  {price.buy_hope_min} ~ {price.buy_hope_max}
                </dd>
              </>
            )}
            {price.sell_target_min != null && (
              <>
                <dt>매도 목표 범위</dt>
                <dd>
                  {price.sell_target_min} ~ {price.sell_target_max}
                </dd>
              </>
            )}
          </dl>
        </section>
      ) : null}

      <section aria-label="강세/약세 근거">
        <div>
          <h3>강세 근거</h3>
          <ul>
            {summary.bull_arguments.map((arg, i) => (
              <li key={`bull-${i}`}>{arg.text}</li>
            ))}
          </ul>
        </div>
        <div>
          <h3>약세 근거</h3>
          <ul>
            {summary.bear_arguments.map((arg, i) => (
              <li key={`bear-${i}`}>{arg.text}</li>
            ))}
          </ul>
        </div>
      </section>

      {summary.warnings && summary.warnings.length > 0 ? (
        <section aria-label="경고" role="alert">
          <h3>경고</h3>
          <ul>
            {summary.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <CitedStageSidebar
        links={summary.summary_stage_links}
        stages={data.stages}
        onJumpToStage={onJumpToStage}
      />

      <button type="button" disabled aria-disabled="true">
        실행 의사결정 세션으로 승격 (Phase 4 준비 중)
      </button>
    </div>
  );
}

import { STAGE_VERDICT_LABEL } from "../i18n/ko";
import type { FundamentalsSignals, StageAnalysis } from "../api/types";

interface Props {
  stage: StageAnalysis | null;
}

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

export default function ResearchFundamentalsTab({ stage }: Props) {
  if (!stage) return <p>펀더멘털 단계 데이터가 없습니다.</p>;
  if (stage.verdict === "unavailable")
    return <p>펀더멘털 단계 데이터를 가져올 수 없습니다.</p>;
  const s = stage.signals as FundamentalsSignals;

  return (
    <div>
      <header>
        <span data-verdict={stage.verdict}>
          {STAGE_VERDICT_LABEL[stage.verdict]}
        </span>
        <progress value={stage.confidence} max={100}>
          {stage.confidence}%
        </progress>
      </header>

      <dl>
        <dt>PER</dt>
        <dd>{fmt(s.per)}</dd>
        <dt>PBR</dt>
        <dd>{fmt(s.pbr)}</dd>
        <dt>PEG</dt>
        <dd>{fmt(s.peg)}</dd>
        <dt>EV/EBITDA</dt>
        <dd>{fmt(s.ev_ebitda)}</dd>
        <dt>시가총액</dt>
        <dd>{fmt(s.market_cap)}</dd>
        <dt>섹터</dt>
        <dd>{fmt(s.sector)}</dd>
        <dt>피어 PER 상대</dt>
        <dd>{fmt(s.relative_per_vs_peers)}</dd>
        <dt>애널리스트 컨센서스</dt>
        <dd>{fmt(s.analyst_consensus)}</dd>
        <dt>내부자 흐름</dt>
        <dd>{fmt(s.insider_flow)}</dd>
      </dl>

      {(s.disclosures?.length ?? 0) > 0 && (
        <section aria-label="공시">
          <h4>공시</h4>
          <ul>
            {(s.disclosures ?? []).map((d, i) => (
              <li key={i}>
                {d.url ? (
                  <a href={d.url} target="_blank" rel="noreferrer noopener">
                    {d.title}
                  </a>
                ) : (
                  d.title
                )}
                {d.reported_at ? ` · ${d.reported_at}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

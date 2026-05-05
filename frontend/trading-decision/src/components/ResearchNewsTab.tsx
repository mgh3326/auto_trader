import { STAGE_VERDICT_LABEL } from "../i18n/ko";
import type { NewsSignals, StageAnalysis } from "../api/types";

interface Props {
  stage: StageAnalysis | null;
}

function isStale(publishedAt?: string): boolean {
  if (!publishedAt) return false;
  const ageMs = Date.now() - new Date(publishedAt).getTime();
  return ageMs > 6 * 60 * 60 * 1000;
}

export default function ResearchNewsTab({ stage }: Props) {
  if (!stage) return <p>뉴스 단계 데이터가 없습니다.</p>;
  if (stage.verdict === "unavailable")
    return <p>뉴스 단계 데이터를 가져올 수 없습니다.</p>;
  const s = stage.signals as NewsSignals;

  const oldest = s.articles?.reduce<string | undefined>((acc, a) => {
    if (!a.published_at) return acc;
    if (!acc) return a.published_at;
    return new Date(a.published_at) < new Date(acc) ? a.published_at : acc;
  }, undefined);

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
        <dt>헤드라인 수</dt>
        <dd>{s.headline_count ?? "—"}</dd>
        <dt>감성 점수</dt>
        <dd>{s.sentiment_score ?? "—"}</dd>
      </dl>

      {(s.top_themes?.length ?? 0) > 0 && (
        <section aria-label="주요 테마">
          <ul>
            {(s.top_themes ?? []).map((theme) => (
              <li key={theme}>{theme}</li>
            ))}
          </ul>
        </section>
      )}

      {isStale(oldest) && (
        <p role="alert">가장 오래된 기사가 6시간을 초과했습니다.</p>
      )}

      {(s.articles?.length ?? 0) > 0 && (
        <section aria-label="기사 목록">
          <ul>
            {(s.articles ?? []).map((a, i) => (
              <li key={`${a.url ?? i}`}>
                {a.url ? (
                  <a href={a.url} target="_blank" rel="noreferrer noopener">
                    {a.title ?? a.url}
                  </a>
                ) : (
                  a.title ?? "—"
                )}
                {a.source ? ` · ${a.source}` : ""}
                {a.published_at ? ` · ${a.published_at}` : ""}
                {a.sentiment ? ` · ${a.sentiment}` : ""}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

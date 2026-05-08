// frontend/invest/src/pages/DiscoverIssueDetailPage.tsx
import { Link, useParams } from "react-router-dom";
import { DesktopHeader } from "../desktop/DesktopHeader";
import { MobileShell } from "../mobile/MobileShell";
import { useViewport } from "../hooks/useViewport";
import { Card, Pill } from "../ds";
import { describeDirection } from "../components/discover/severity";
import { formatRelativeTime } from "../format/relativeTime";
import { useNewsIssues, type NewsIssuesState } from "../hooks/useNewsIssues";
import type {
  IssueDirection,
  MarketIssue,
  MarketIssueRelatedSymbol,
} from "../types/newsIssues";

export interface DiscoverIssueDetailPageProps {
  state?: NewsIssuesState;
  reload?: () => void;
}

const DIRECTION_TONE: Record<IssueDirection, "gain" | "loss" | "warn" | "paper"> = {
  up: "gain",
  down: "loss",
  mixed: "warn",
  neutral: "paper",
};

const DIRECTION_COPY: Record<IssueDirection, string> = {
  up: "관련 종목·섹터에 긍정 모멘텀으로 해석될 수 있어요.",
  down: "관련 종목·섹터에 부담 요인으로 해석될 수 있어요.",
  mixed: "수혜와 부담이 함께 나타날 수 있어 가격 반응을 나눠 봐야 해요.",
  neutral: "방향성은 아직 뚜렷하지 않아 추가 뉴스 확인이 필요해요.",
};

export function DiscoverIssueDetailPage(props: DiscoverIssueDetailPageProps = {}) {
  const params = useParams<{ issueId: string }>();
  const live = useNewsIssues(
    {
      market: "all",
      windowHours: 24,
      limit: 20,
    },
    { enabled: props.state === undefined },
  );
  const state = props.state ?? live.state;
  const reload = props.reload ?? live.reload;
  const issueId = params.issueId ?? "";

  return (
    <DetailShell>
      <IssueDetailBody state={state} reload={reload} issueId={issueId} />
    </DetailShell>
  );
}

function DetailShell({ children }: { children: React.ReactNode }) {
  const viewport = useViewport();
  if (viewport === "mobile") {
    return (
      <MobileShell title="이슈">
        <div style={{ padding: "16px 16px 32px" }}>{children}</div>
      </MobileShell>
    );
  }
  return (
    <div style={{ minHeight: "100vh", background: "var(--bg-alt)" }}>
      <DesktopHeader />
      <main
        style={{
          maxWidth: 800,
          margin: "0 auto",
          padding: "24px 28px 64px",
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        {children}
      </main>
    </div>
  );
}

function IssueDetailBody({
  state,
  reload,
  issueId,
}: {
  state: NewsIssuesState;
  reload: () => void;
  issueId: string;
}) {
  if (state.status === "loading") {
    return <div style={{ color: "var(--fg-3)" }}>불러오는 중…</div>;
  }
  if (state.status === "error") {
    return (
      <div>
        <div style={{ color: "var(--danger)", marginBottom: 8 }}>잠시 후 다시 시도해 주세요.</div>
        <button
          type="button"
          onClick={reload}
          style={{
            padding: "6px 12px",
            borderRadius: 8,
            border: "1px solid var(--border)",
            background: "var(--surface)",
            color: "var(--fg-1)",
            cursor: "pointer",
            fontFamily: "inherit",
            fontSize: 12,
          }}
        >
          재시도
        </button>
        <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 8 }}>{state.message}</div>
      </div>
    );
  }

  const item = state.data.items.find((i) => i.id === issueId);
  if (!item) {
    return (
      <div>
        <p style={{ marginTop: 0, color: "var(--fg-1)" }}>
          이슈를 찾을 수 없습니다. 시간이 지나 목록에서 빠졌을 수 있어요.
        </p>
        <Link
          to="/discover"
          style={{ color: "var(--accent-press)", fontWeight: 700, textDecoration: "none" }}
        >
          발견으로 돌아가기
        </Link>
      </div>
    );
  }

  return <IssueDetailView item={item} />;
}

function IssueDetailView({ item }: { item: MarketIssue }) {
  const indicator = describeDirection(item.direction);
  const time = formatRelativeTime(item.updated_at);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Link
        to="/discover"
        data-testid="issue-detail-back"
        style={{
          fontSize: 12,
          color: "var(--fg-3)",
          textDecoration: "none",
          fontWeight: 600,
          alignSelf: "flex-start",
        }}
      >
        ← 발견
      </Link>

      <header style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
          <span aria-label={indicator.label} role="img" style={{ color: indicator.color, fontWeight: 700 }}>
            {indicator.glyph}
          </span>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em" }}>
            {item.issue_title}
          </h1>
        </div>
        {item.summary && (
          <p style={{ margin: 0, fontSize: 14, color: "var(--fg-2)", lineHeight: 1.6 }}>{item.summary}</p>
        )}
        <div style={{ display: "flex", gap: 8, fontSize: 11, color: "var(--fg-3)" }}>
          <span>{item.source_count}개 출처</span>
          <span>· 기사 {item.article_count}개</span>
          {time && <span>· {time}</span>}
        </div>
      </header>

      <ImpactSection direction={item.direction} sectors={item.related_sectors} />
      <RelatedSymbolsSection symbols={item.related_symbols} />
      <ArticlesSection articles={item.articles} />

      <Card soft padded style={{ padding: 12, fontSize: 12 }}>
        <strong style={{ display: "block", marginBottom: 4, color: "var(--fg)" }}>꼭 알아두세요</strong>
        <span style={{ color: "var(--fg-3)" }}>
          이 화면은 read-only 정보입니다. 매수/매도 주문이나 자동 추천을 제공하지 않습니다.
        </span>
      </Card>
    </div>
  );
}

function ImpactSection({
  direction,
  sectors,
}: {
  direction: IssueDirection;
  sectors: readonly string[];
}) {
  const labels = sectors.length > 0 ? sectors : ["관련 시장"];
  const tone = DIRECTION_TONE[direction];
  const copy = DIRECTION_COPY[direction];
  return (
    <section aria-labelledby="impact-heading">
      <h2 id="impact-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700, color: "var(--fg)" }}>
        어떤 영향을 줄까?
      </h2>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 8,
          marginTop: 8,
        }}
      >
        {labels.map((label) => (
          <div
            key={label}
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              flexWrap: "wrap",
              padding: "10px 14px",
              borderRadius: 12,
              background:
                tone === "gain"
                  ? "var(--gain-soft)"
                  : tone === "loss"
                    ? "var(--loss-soft)"
                    : tone === "warn"
                      ? "var(--warn-soft)"
                      : "var(--surface-2)",
              fontSize: 13,
            }}
          >
            <strong style={{ color: "var(--fg)" }}>{label}</strong>
            <span style={{ color: "var(--fg-2)" }}>{copy}</span>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 12, fontSize: 11, color: "var(--fg-3)" }}>
        뉴스 기반 참고 정보이며 매매 추천이 아닙니다.
      </div>
    </section>
  );
}

function RelatedSymbolsSection({ symbols }: { symbols: readonly MarketIssueRelatedSymbol[] }) {
  return (
    <section aria-labelledby="symbols-heading">
      <h2 id="symbols-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700, color: "var(--fg)" }}>
        관련 종목
      </h2>
      {symbols.length > 0 ? (
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
          {symbols.map((sym) => (
            <li
              key={`${sym.market}:${sym.symbol}`}
              style={{
                padding: "10px 12px",
                background: "var(--surface)",
                border: "1px solid var(--border)",
                borderRadius: 12,
                fontSize: 13,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: 10,
              }}
            >
              <strong style={{ color: "var(--fg)" }}>{sym.canonical_name || sym.symbol}</strong>
              <span style={{ color: "var(--fg-3)", fontSize: 12, display: "flex", gap: 6, alignItems: "center" }}>
                <span style={{ fontFamily: "var(--font-mono)" }}>{sym.symbol}</span>
                <span aria-hidden style={{ width: 2, height: 2, background: "var(--fg-4)", borderRadius: 999 }} />
                <span>{sym.mention_count}회 언급</span>
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 8 }}>관련 종목 분석은 준비 중입니다.</div>
      )}
    </section>
  );
}

function ArticlesSection({
  articles,
}: {
  articles: readonly MarketIssue["articles"][number][];
}) {
  return (
    <section aria-labelledby="articles-heading">
      <h2 id="articles-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700, color: "var(--fg)" }}>
        관련 뉴스
      </h2>
      {articles.length > 0 ? (
        <ul
          style={{
            listStyle: "none",
            margin: "8px 0 0",
            padding: 0,
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          {articles.map((article) => (
            <li
              key={article.id}
              style={{
                padding: 12,
                background: "var(--surface)",
                border: "1px solid var(--border)",
                borderRadius: 12,
                boxShadow: "var(--shadow-1)",
              }}
            >
              <a
                href={article.url}
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--fg)", textDecoration: "none", fontWeight: 700, fontSize: 14 }}
              >
                {article.title}
              </a>
              <div style={{ marginTop: 4, fontSize: 11, color: "var(--fg-3)" }}>
                {article.source ?? article.feed_source ?? "출처 미상"}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 8 }}>관련 뉴스 원문 링크가 없습니다.</div>
      )}
    </section>
  );
}


import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { MobileShell } from "../../mobile/MobileShell";
import { fetchFeedNews } from "../../api/feedNews";
import { fetchFeedResearch } from "../../api/feedResearch";
import type { FeedNewsResponse } from "../../types/feedNews";
import type { FeedResearchResponse } from "../../types/feedResearch";
import { NewsTabs, type FeedContentTab } from "../../components/news/NewsTabs";
import { NewsListItem } from "../../components/news/NewsListItem";
import { ResearchListItem } from "../../components/news/ResearchListItem";

function emptyMessage(reason: string | null | undefined): string {
  if (reason === "no_holdings") return "보유 종목이 없습니다.";
  if (reason === "no_watchlist") return "관심 종목이 없습니다.";
  if (reason === "no_matching_news") return "조건에 맞는 뉴스가 없습니다.";
  return "표시할 뉴스가 없습니다.";
}

function getParam(searchParams: URLSearchParams, key: string): string | undefined {
  return searchParams.get(key)?.trim() || undefined;
}

export function MobileFeedNewsPage() {
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<FeedContentTab>("top");
  const [data, setData] = useState<FeedNewsResponse | undefined>();
  const [researchData, setResearchData] = useState<FeedResearchResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [selectedId, setSelectedId] = useState<number | null>(null);

  useEffect(() => {
    let cancel = false;
    setErr(undefined);
    setSelectedId(null);

    if (tab === "research") {
      setResearchData(undefined);
      fetchFeedResearch({
        tab: "latest",
        limit: 30,
        source: getParam(searchParams, "source"),
        symbol: getParam(searchParams, "symbol"),
        analyst: getParam(searchParams, "analyst"),
        category: getParam(searchParams, "category"),
        query: getParam(searchParams, "query"),
        fromDate: getParam(searchParams, "fromDate"),
        toDate: getParam(searchParams, "toDate"),
      })
        .then((r) => !cancel && setResearchData(r))
        .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    } else {
      setData(undefined);
      fetchFeedNews({ tab, limit: 30 })
        .then((r) => !cancel && setData(r))
        .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    }

    return () => {
      cancel = true;
    };
  }, [tab, searchParams]);

  const researchMode = tab === "research";
  const issueById = new Map((data?.issues ?? []).map((i) => [i.id, i] as const));
  const loading = researchMode ? !researchData && !err : !data && !err;
  const empty = researchMode
    ? Boolean(researchData && researchData.items.length === 0)
    : Boolean(data && data.items.length === 0);

  return (
    <MobileShell title="뉴스">
      <div data-testid="feed-center" style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 10 }}>
        <NewsTabs value={tab} onChange={setTab} variant="pill-row" />

        {err && <div style={{ color: "var(--danger)" }}>오류: {err}</div>}
        {loading && (
          <div data-testid={researchMode ? "feed-research-loading" : "feed-news-loading"} style={{ color: "var(--fg-3)" }}>
            {researchMode ? "리서치 자료를 불러오는 중입니다…" : "최신 뉴스를 불러오는 중입니다…"}
          </div>
        )}
        {empty && (
          <div data-testid={researchMode ? "feed-research-empty" : "feed-news-empty"} style={{ color: "var(--fg-3)" }}>
            {researchMode ? "표시할 리서치 리포트가 없습니다." : emptyMessage(data?.meta?.emptyReason)}
          </div>
        )}

        {researchMode ? (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
            {(researchData?.items ?? []).map((it) => (
              <ResearchListItem key={it.id} item={it} variant="mobile" />
            ))}
          </ul>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
            {(data?.items ?? []).map((it) => {
              const open = selectedId === it.id;
              const linkedIssue = it.issueId ? issueById.get(it.issueId) : undefined;
              return (
                <NewsListItem
                  key={it.id}
                  item={it}
                  issue={linkedIssue}
                  open={open}
                  onToggle={() => setSelectedId(open ? null : it.id)}
                  variant="mobile"
                />
              );
            })}
          </ul>
        )}
      </div>
    </MobileShell>
  );
}

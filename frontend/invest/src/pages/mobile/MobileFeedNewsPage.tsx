import { useEffect, useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { fetchFeedNews } from "../../api/feedNews";
import type { FeedNewsResponse, FeedTab } from "../../types/feedNews";
import { NewsTabs } from "../../components/news/NewsTabs";
import { NewsListItem } from "../../components/news/NewsListItem";
import { issueNamespaceForTab } from "../../components/news/issueLink";

function emptyMessage(reason: string | null | undefined): string {
  if (reason === "no_holdings") return "보유 종목이 없습니다.";
  if (reason === "no_watchlist") return "관심 종목이 없습니다.";
  if (reason === "no_matching_news") return "조건에 맞는 뉴스가 없습니다.";
  return "표시할 뉴스가 없습니다.";
}

export function MobileFeedNewsPage() {
  const [tab, setTab] = useState<FeedTab>("top");
  const [data, setData] = useState<FeedNewsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [selectedId, setSelectedId] = useState<number | null>(null);

  useEffect(() => {
    let cancel = false;
    setData(undefined);
    setErr(undefined);
    fetchFeedNews({ tab, limit: 30 })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => {
      cancel = true;
    };
  }, [tab]);

  const issueById = new Map((data?.issues ?? []).map((i) => [i.id, i] as const));
  const issueMarket = issueNamespaceForTab(tab);
  const loading = !data && !err;
  const empty = Boolean(data && data.items.length === 0);

  return (
    <MobileShell title="뉴스">
      <div data-testid="feed-center" style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 10 }}>
        <NewsTabs value={tab} onChange={setTab} variant="pill-row" />

        {err && <div style={{ color: "var(--danger)" }}>오류: {err}</div>}
        {loading && (
          <div data-testid="feed-news-loading" style={{ color: "var(--fg-3)" }}>
            최신 뉴스를 불러오는 중입니다…
          </div>
        )}
        {empty && (
          <div data-testid="feed-news-empty" style={{ color: "var(--fg-3)" }}>
            {emptyMessage(data?.meta?.emptyReason)}
          </div>
        )}

        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
          {(data?.items ?? []).map((it) => {
            const open = selectedId === it.id;
            const linkedIssue = it.issueId ? issueById.get(it.issueId) : undefined;
            return (
              <NewsListItem
                key={it.id}
                item={it}
                issue={linkedIssue}
                issueMarket={issueMarket}
                open={open}
                onToggle={() => setSelectedId(open ? null : it.id)}
                variant="mobile"
              />
            );
          })}
        </ul>
      </div>
    </MobileShell>
  );
}

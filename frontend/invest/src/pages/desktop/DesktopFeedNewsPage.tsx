import { useEffect, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { useViewport } from "../../hooks/useViewport";
import { fetchFeedNews } from "../../api/feedNews";
import type { FeedNewsResponse, FeedTab } from "../../types/feedNews";
import { NewsTabs } from "../../components/news/NewsTabs";
import { NewsListItem } from "../../components/news/NewsListItem";
import { MobileFeedNewsPage } from "../mobile/MobileFeedNewsPage";

function emptyMessage(reason: string | null | undefined): string {
  if (reason === "no_holdings") return "보유 종목이 없습니다.";
  if (reason === "no_watchlist") return "관심 종목이 없습니다.";
  if (reason === "no_matching_news") return "조건에 맞는 뉴스가 없습니다.";
  return "표시할 뉴스가 없습니다.";
}

export function FeedNewsRoute() {
  const viewport = useViewport();
  return viewport === "mobile" ? <MobileFeedNewsPage /> : <DesktopFeedNewsPage />;
}

export function DesktopFeedNewsPage() {
  const panel = useAccountPanel();
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
  const loading = !data && !err;
  const empty = Boolean(data && data.items.length === 0);

  return (
    <DesktopShell
      center={
        <>
          <header>
            <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800, letterSpacing: "-0.02em" }}>뉴스</h1>
            <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--fg-3)" }}>
              보유 · 관심 종목 관련 기사를 우선 보여드립니다.
            </p>
          </header>

          <NewsTabs value={tab} onChange={setTab} />

          <div data-testid="feed-center">
            {err && <div style={{ color: "var(--danger)", marginBottom: 12 }}>오류: {err}</div>}
            {loading && (
              <div data-testid="feed-news-loading" style={{ padding: 16, color: "var(--fg-3)" }}>
                최신 뉴스를 불러오는 중입니다…
              </div>
            )}
            {empty && (
              <div data-testid="feed-news-empty" style={{ padding: 16, color: "var(--fg-3)" }}>
                {emptyMessage(data?.meta?.emptyReason)}
              </div>
            )}
            <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
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
                  />
                );
              })}
            </ul>
          </div>
        </>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} onRefresh={panel.reload} />}
    />
  );
}

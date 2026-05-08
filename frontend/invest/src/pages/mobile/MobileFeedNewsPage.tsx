import { useEffect, useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { fetchFeedNews } from "../../api/feedNews";
import type { FeedNewsResponse, FeedTab } from "../../types/feedNews";
import { NewsTabs } from "../../components/news/NewsTabs";
import { NewsCard } from "../../components/news/NewsCard";

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

  return (
    <MobileShell title="뉴스">
      <div data-testid="feed-center" style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 10 }}>
        <NewsTabs value={tab} onChange={setTab} variant="pill-row" />

        {err && <div style={{ color: "var(--danger)" }}>오류: {err}</div>}
        {data?.meta?.emptyReason === "no_holdings" && (
          <div style={{ color: "var(--fg-3)" }}>보유 종목이 없습니다.</div>
        )}
        {data?.meta?.emptyReason === "no_watchlist" && (
          <div style={{ color: "var(--fg-3)" }}>관심 종목이 없습니다.</div>
        )}

        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
          {(data?.items ?? []).map((it) => {
            const open = selectedId === it.id;
            const linkedIssue = it.issueId ? issueById.get(it.issueId) : undefined;
            return (
              <NewsCard
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
    </MobileShell>
  );
}

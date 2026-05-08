import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { fetchFeedNews } from "../../api/feedNews";
import type { FeedNewsResponse, FeedTab } from "../../types/feedNews";

const TABS: { key: FeedTab; label: string }[] = [
  { key: "top", label: "주요" }, { key: "latest", label: "최신" }, { key: "hot", label: "핫이슈" },
  { key: "holdings", label: "보유" }, { key: "watchlist", label: "관심" },
  { key: "kr", label: "국내" }, { key: "us", label: "해외" }, { key: "crypto", label: "크립토" },
];

export function DesktopFeedNewsPage() {
  const panel = useAccountPanel();
  const [tab, setTab] = useState<FeedTab>("top");
  const [data, setData] = useState<FeedNewsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [selectedId, setSelectedId] = useState<number | null>(null);

  useEffect(() => {
    let cancel = false;
    setData(undefined); setErr(undefined);
    fetchFeedNews({ tab, limit: 30 })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, [tab]);

  const issueById = new Map((data?.issues ?? []).map((i) => [i.id, i] as const));

  return (
    <DesktopShell
      left={
        <nav style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {TABS.map((t) => (
            <button
              key={t.key}
              data-testid={`tab-${t.key}`}
              onClick={() => setTab(t.key)}
              style={{
                textAlign: "left", padding: "6px 10px", borderRadius: 6,
                background: tab === t.key ? "var(--surface-2, #1c1e24)" : "transparent",
                color: "#e8eaf0", border: "none", cursor: "pointer", fontSize: 13,
              }}
            >
              {t.label}
            </button>
          ))}
        </nav>
      }
      center={
        <div data-testid="feed-center">
          {err && <div style={{ color: "#f59e9e", marginBottom: 12 }}>오류: {err}</div>}
          {data?.meta?.emptyReason === "no_holdings" && (
            <div style={{ padding: 16, color: "#9ba0ab" }}>보유 종목이 없습니다.</div>
          )}
          {data?.meta?.emptyReason === "no_watchlist" && (
            <div style={{ padding: 16, color: "#9ba0ab" }}>관심 종목이 없습니다.</div>
          )}
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {(data?.items ?? []).map((it) => {
              const open = selectedId === it.id;
              const linkedIssue = it.issueId ? issueById.get(it.issueId) : undefined;
              return (
                <li
                  key={it.id}
                  data-testid="feed-item"
                  data-relation={it.relation}
                  style={{ padding: 12, borderRadius: 10, background: "var(--surface, #15181f)" }}
                >
                  <button
                    onClick={() => setSelectedId(open ? null : it.id)}
                    style={{ background: "none", border: "none", color: "#e8eaf0", textAlign: "left", padding: 0, cursor: "pointer", width: "100%" }}
                  >
                    <div style={{ fontSize: 14, fontWeight: 600 }}>{it.title}</div>
                    <div style={{ fontSize: 11, color: "#9ba0ab", marginTop: 4 }}>
                      {it.publisher ?? "—"} · {it.market.toUpperCase()}
                      {it.relation !== "none" && <span style={{ marginLeft: 8 }}>[{it.relation}]</span>}
                    </div>
                  </button>
                  {linkedIssue && (
                    <Link
                      to={`/app/discover/issues/${linkedIssue.id}`}
                      data-testid="feed-item-issue-chip"
                      data-issue-id={linkedIssue.id}
                      aria-label={`이슈 링크: ${linkedIssue.issue_title}`}
                      onClick={(e) => e.stopPropagation()}
                      style={{
                        marginTop: 6,
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                        padding: "2px 8px",
                        borderRadius: 999,
                        background: "var(--surface-2, #1c1e24)",
                        color: "#cfd2da",
                        fontSize: 11,
                        textDecoration: "none",
                        maxWidth: "100%",
                      }}
                    >
                      <span aria-hidden style={{ fontSize: 9 }}>●</span>
                      <span
                        style={{
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        이슈 · {linkedIssue.issue_title}
                      </span>
                    </Link>
                  )}
                  {open && it.summarySnippet && (
                    <div style={{ marginTop: 8, fontSize: 13, color: "#cfd2da" }}>{it.summarySnippet}</div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}

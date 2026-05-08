import { useEffect, useState } from "react";
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

  return (
    <DesktopShell
      left={
        <nav style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {TABS.map((t) => {
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                data-testid={`tab-${t.key}`}
                onClick={() => setTab(t.key)}
                style={{
                  textAlign: "left", padding: "6px 10px", borderRadius: 6,
                  background: active ? "var(--surface-2)" : "transparent",
                  color: active ? "var(--fg)" : "var(--fg-2)",
                  fontWeight: active ? 700 : 500,
                  border: "none", cursor: "pointer", fontSize: 13,
                }}
              >
                {t.label}
              </button>
            );
          })}
        </nav>
      }
      center={
        <div data-testid="feed-center">
          {err && <div style={{ color: "var(--danger)", marginBottom: 12 }}>오류: {err}</div>}
          {data?.meta?.emptyReason === "no_holdings" && (
            <div style={{ padding: 16, color: "var(--fg-3)" }}>보유 종목이 없습니다.</div>
          )}
          {data?.meta?.emptyReason === "no_watchlist" && (
            <div style={{ padding: 16, color: "var(--fg-3)" }}>관심 종목이 없습니다.</div>
          )}
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {(data?.items ?? []).map((it) => {
              const open = selectedId === it.id;
              return (
                <li
                  key={it.id}
                  data-testid="feed-item"
                  data-relation={it.relation}
                  style={{
                    padding: 12, borderRadius: 10,
                    background: "var(--surface)",
                    border: "1px solid var(--border)",
                    boxShadow: "var(--shadow-1)",
                  }}
                >
                  <button
                    onClick={() => setSelectedId(open ? null : it.id)}
                    style={{ background: "none", border: "none", color: "var(--fg)", textAlign: "left", padding: 0, cursor: "pointer", width: "100%" }}
                  >
                    <div style={{ fontSize: 14, fontWeight: 600 }}>{it.title}</div>
                    <div style={{ fontSize: 11, color: "var(--fg-3)", marginTop: 4 }}>
                      {it.publisher ?? "—"} · {it.market.toUpperCase()}
                      {it.relation !== "none" && <span style={{ marginLeft: 8 }}>[{it.relation}]</span>}
                    </div>
                  </button>
                  {open && it.summarySnippet && (
                    <div style={{ marginTop: 8, fontSize: 13, color: "var(--fg-2)" }}>{it.summarySnippet}</div>
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

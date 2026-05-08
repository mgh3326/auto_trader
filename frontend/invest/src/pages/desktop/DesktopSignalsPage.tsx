import { useEffect, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { useViewport } from "../../hooks/useViewport";
import { fetchSignals } from "../../api/signals";
import type { SignalsResponse, SignalTab, SignalCard as SignalCardData } from "../../types/signals";
import { SignalCard } from "../../components/signals/SignalCard";
import { Card, Pill } from "../../ds";
import { formatRelativeTime } from "../../format/relativeTime";
import { MobileSignalsPage } from "../mobile/MobileSignalsPage";

const TABS: { key: SignalTab; label: string }[] = [
  { key: "mine", label: "내 투자 / 관심" },
  { key: "kr", label: "국내" },
  { key: "us", label: "해외" },
  { key: "crypto", label: "크립토" },
];

export function SignalsRoute() {
  return useViewport() === "mobile" ? <MobileSignalsPage /> : <DesktopSignalsPage />;
}

export function DesktopSignalsPage() {
  const panel = useAccountPanel();
  const [tab, setTab] = useState<SignalTab>("mine");
  const [data, setData] = useState<SignalsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [selected, setSelected] = useState<SignalCardData | null>(null);

  useEffect(() => {
    let cancel = false;
    setData(undefined);
    setErr(undefined);
    setSelected(null);
    fetchSignals({ tab, limit: 30 })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => {
      cancel = true;
    };
  }, [tab]);

  return (
    <DesktopShell
      left={
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <nav style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {TABS.map((t) => {
              const active = tab === t.key;
              return (
                <button
                  key={t.key}
                  data-testid={`signal-tab-${t.key}`}
                  onClick={() => setTab(t.key)}
                  style={{
                    textAlign: "left",
                    padding: "6px 10px",
                    borderRadius: 8,
                    background: active ? "var(--surface-2)" : "transparent",
                    color: active ? "var(--fg)" : "var(--fg-2)",
                    fontWeight: active ? 700 : 600,
                    border: "none",
                    cursor: "pointer",
                    fontSize: 13,
                    fontFamily: "inherit",
                  }}
                >
                  {t.label}
                </button>
              );
            })}
          </nav>
          {err && <div style={{ color: "var(--danger)", fontSize: 12 }}>오류: {err}</div>}
          {data?.meta.emptyReason && (
            <div style={{ fontSize: 12, color: "var(--fg-3)" }}>결과 없음 ({data.meta.emptyReason})</div>
          )}
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {(data?.items ?? []).map((s) => (
              <li key={s.id}>
                <SignalCard
                  signal={s}
                  selected={selected?.id === s.id}
                  onSelect={() => setSelected(s)}
                />
              </li>
            ))}
          </ul>
        </div>
      }
      center={
        <Card data-testid="signal-detail" padded={false} style={{ padding: 24 }}>
          {!selected ? (
            <div style={{ color: "var(--fg-3)" }}>시그널을 선택하세요.</div>
          ) : (
            <SignalDetail signal={selected} />
          )}
        </Card>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} onRefresh={panel.reload} />}
    />
  );
}

function SignalDetail({ signal }: { signal: SignalCardData }) {
  const ago = formatRelativeTime(signal.generatedAt) ?? "방금";
  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <h2 style={{ fontSize: 20, fontWeight: 800, margin: 0, letterSpacing: "-0.02em" }}>{signal.title}</h2>
        {signal.relation !== "none" && (
          <Pill tone="accent" size="sm">
            {signal.relation === "held" ? "보유" : signal.relation === "watchlist" ? "관심" : "보유·관심"}
          </Pill>
        )}
      </div>
      <div style={{ fontSize: 12, color: "var(--fg-3)", marginTop: 6, fontFeatureSettings: '"tnum"' }}>
        {signal.market.toUpperCase()} · {signal.decisionLabel ?? "neutral"}
        {signal.confidence != null && ` · 신뢰도 ${signal.confidence}%`}
        {` · ${ago}`}
      </div>
      {signal.summary && (
        <p style={{ marginTop: 14, color: "var(--fg-1)", fontSize: 14, lineHeight: 1.6 }}>{signal.summary}</p>
      )}
      {signal.rationale && (
        <details style={{ marginTop: 14 }}>
          <summary style={{ cursor: "pointer", color: "var(--fg-2)", fontSize: 13 }}>근거</summary>
          <pre
            style={{
              whiteSpace: "pre-wrap",
              fontSize: 12,
              fontFamily: "var(--font-mono)",
              background: "var(--surface-2)",
              padding: 12,
              borderRadius: 8,
              color: "var(--fg-1)",
              marginTop: 8,
            }}
          >
            {signal.rationale}
          </pre>
        </details>
      )}
      {signal.relatedSymbols.length > 0 && (
        <div style={{ marginTop: 12, fontSize: 12, color: "var(--fg-3)" }}>
          관련 종목: {signal.relatedSymbols.map((r) => r.displayName).join(", ")}
        </div>
      )}
    </div>
  );
}

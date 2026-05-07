import { useEffect, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { fetchSignals } from "../../api/signals";
import type { SignalsResponse, SignalTab, SignalCard } from "../../types/signals";

const TABS: { key: SignalTab; label: string }[] = [
  { key: "mine", label: "내 투자 / 관심" },
  { key: "kr", label: "국내" },
  { key: "us", label: "해외" },
  { key: "crypto", label: "크립토" },
];

export function DesktopSignalsPage() {
  const panel = useAccountPanel();
  const [tab, setTab] = useState<SignalTab>("mine");
  const [data, setData] = useState<SignalsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [selected, setSelected] = useState<SignalCard | null>(null);

  useEffect(() => {
    let cancel = false;
    setData(undefined); setErr(undefined); setSelected(null);
    fetchSignals({ tab, limit: 30 })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, [tab]);

  return (
    <DesktopShell
      left={
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <nav style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {TABS.map((t) => (
              <button
                key={t.key} data-testid={`signal-tab-${t.key}`}
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
          {err && <div style={{ color: "#f59e9e" }}>오류: {err}</div>}
          {data?.meta.emptyReason && <div style={{ fontSize: 12, color: "#9ba0ab" }}>결과 없음 ({data.meta.emptyReason})</div>}
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
            {(data?.items ?? []).map((s) => (
              <li key={s.id}>
                <button
                  data-testid="signal-list-item"
                  data-relation={s.relation}
                  onClick={() => setSelected(s)}
                  style={{
                    width: "100%", textAlign: "left", padding: 10, borderRadius: 8,
                    background: selected?.id === s.id ? "var(--surface-2, #1c1e24)" : "var(--surface, #15181f)",
                    border: "none", color: "#e8eaf0", cursor: "pointer",
                  }}
                >
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{s.title}</div>
                  <div style={{ fontSize: 11, color: "#9ba0ab" }}>
                    {s.market.toUpperCase()} · {s.decisionLabel ?? "neutral"}
                    {s.confidence != null && ` · ${s.confidence}%`}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>
      }
      center={
        <section data-testid="signal-detail" style={{ padding: 24, borderRadius: 12, background: "var(--surface, #15181f)" }}>
          {!selected ? (
            <div style={{ color: "#9ba0ab" }}>시그널을 선택하세요.</div>
          ) : (
            <>
              <h2 style={{ fontSize: 18, marginTop: 0 }}>{selected.title}</h2>
              <div style={{ fontSize: 12, color: "#9ba0ab" }}>
                {selected.market.toUpperCase()} · {selected.decisionLabel ?? "neutral"}
                {selected.confidence != null && ` · 신뢰도 ${selected.confidence}%`}
                {` · ${new Date(selected.generatedAt).toLocaleString("ko-KR")}`}
              </div>
              {selected.summary && <p style={{ marginTop: 12 }}>{selected.summary}</p>}
              {selected.rationale && (
                <details style={{ marginTop: 12 }}>
                  <summary style={{ cursor: "pointer", color: "#9ba0ab" }}>근거</summary>
                  <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{selected.rationale}</pre>
                </details>
              )}
              {selected.relatedSymbols.length > 0 && (
                <div style={{ marginTop: 12, fontSize: 12, color: "#9ba0ab" }}>
                  관련 종목: {selected.relatedSymbols.map((r) => r.displayName).join(", ")}
                </div>
              )}
            </>
          )}
        </section>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}

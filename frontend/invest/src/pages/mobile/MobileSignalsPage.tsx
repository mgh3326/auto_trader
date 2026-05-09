import { useEffect, useState } from "react";
import { MobileShell } from "../../mobile/MobileShell";
import { fetchSignals } from "../../api/signals";
import type { SignalsResponse, SignalTab } from "../../types/signals";
import { SignalCard } from "../../components/signals/SignalCard";

const TABS: { key: SignalTab; label: string }[] = [
  { key: "mine", label: "내 투자" },
  { key: "kr", label: "국내" },
  { key: "us", label: "해외" },
  { key: "crypto", label: "크립토" },
];

export function MobileSignalsPage() {
  const [tab, setTab] = useState<SignalTab>("mine");
  const [data, setData] = useState<SignalsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();

  useEffect(() => {
    let cancel = false;
    setData(undefined);
    setErr(undefined);
    fetchSignals({ tab, limit: 30 })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => {
      cancel = true;
    };
  }, [tab]);

  return (
    <MobileShell title="시그널">
      <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", gap: 6, overflowX: "auto", paddingBottom: 4 }}>
          {TABS.map((t) => {
            const active = t.key === tab;
            return (
              <button
                key={t.key}
                data-testid={`signal-tab-${t.key}`}
                onClick={() => setTab(t.key)}
                style={{
                  flex: "0 0 auto",
                  padding: "6px 12px",
                  borderRadius: 999,
                  border: "none",
                  background: active ? "var(--fg)" : "var(--surface-2)",
                  color: active ? "var(--bg)" : "var(--fg-2)",
                  fontWeight: 600,
                  fontSize: 12,
                  fontFamily: "inherit",
                  whiteSpace: "nowrap",
                  cursor: "pointer",
                }}
              >
                {t.label}
              </button>
            );
          })}
        </div>

        {err && <div style={{ color: "var(--danger)" }}>오류: {err}</div>}
        {data?.meta.emptyReason && (
          <div style={{ color: "var(--fg-3)", fontSize: 13 }}>결과 없음 ({data.meta.emptyReason})</div>
        )}

        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 10 }}>
          {(data?.items ?? []).map((s) => (
            <li key={s.id}>
              <SignalCard signal={s} variant="grid" />
            </li>
          ))}
        </ul>
      </div>
    </MobileShell>
  );
}

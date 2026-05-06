import type { AccountSource } from "../types/invest";

export type ActiveSource = AccountSource | "all";

const LABELS: Record<ActiveSource, string> = {
  all: "전체",
  kis: "KIS",
  upbit: "Upbit",
  toss_manual: "Toss 수동",
  pension_manual: "퇴직연금",
  isa_manual: "ISA",
  kis_mock: "KIS 모의",
  kiwoom_mock: "키움 모의",
  alpaca_paper: "Alpaca",
  db_simulated: "DB 시뮬",
};

export function SourceFilterBar({
  sources,
  active,
  onChange,
}: {
  sources: ActiveSource[];
  active: ActiveSource;
  onChange: (s: ActiveSource) => void;
}) {
  return (
    <div style={{ display: "flex", gap: 6, padding: "0 4px", flexWrap: "wrap" }}>
      {sources.map((s) => {
        const on = s === active;
        return (
          <button
            key={s}
            type="button"
            onClick={() => onChange(s)}
            style={{
              padding: "4px 10px",
              borderRadius: 999,
              background: on ? "var(--text)" : "var(--surface)",
              color: on ? "var(--bg)" : "var(--text)",
              border: "none",
              fontSize: 11,
              cursor: "pointer",
            }}
          >
            {LABELS[s]}
          </button>
        );
      })}
    </div>
  );
}

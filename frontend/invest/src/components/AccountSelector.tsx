import type { AccountSource } from "../types/invest";

export type AccountKey = AccountSource | "all";

const LABELS: Partial<Record<AccountKey, string>> = {
  all: "전체",
  kis: "KIS 실계좌",
  upbit: "Upbit",
  toss_manual: "Toss 수동",
};

export function AccountSelector({
  active,
  onChange,
}: {
  active: AccountKey;
  onChange: (s: AccountKey) => void;
}) {
  const options: AccountKey[] = ["all", "kis", "upbit", "toss_manual"];

  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        padding: "4px 16px",
        overflowX: "auto",
        WebkitOverflowScrolling: "touch",
        scrollbarWidth: "none",
        msOverflowStyle: "none",
      }}
    >
      <style>
        {`div::-webkit-scrollbar { display: none; }`}
      </style>
      {options.map((s) => {
        const on = s === active;
        return (
          <button
            key={s}
            type="button"
            onClick={() => onChange(s)}
            style={{
              flex: "0 0 auto",
              padding: "8px 16px",
              borderRadius: 12,
              background: on ? "var(--text)" : "var(--surface)",
              color: on ? "var(--bg)" : "var(--muted)",
              border: "none",
              fontSize: 13,
              fontWeight: 600,
              cursor: "pointer",
              transition: "all 0.2s",
            }}
          >
            {LABELS[s] || s}
          </button>
        );
      })}
    </div>
  );
}

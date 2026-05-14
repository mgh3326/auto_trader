import type { AccountSource } from "../types/invest";
import { accountSourceMeta } from "../desktop/AccountSourceMeta";

export type AccountKey = AccountSource | "all";

function labelFor(key: AccountKey): string {
  if (key === "all") return "전체";
  return accountSourceMeta(key).label;
}

const OPTIONS: AccountKey[] = [
  "all",
  "kis",
  "upbit",
  "toss_manual",
  "pension_manual",
  "isa_manual",
  "kis_mock",
  "kiwoom_mock",
  "alpaca_paper",
  "db_simulated",
];

export function AccountSelector({
  active,
  onChange,
}: {
  active: AccountKey;
  onChange: (s: AccountKey) => void;
}) {
  const options = OPTIONS;

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
            {labelFor(s)}
          </button>
        );
      })}
    </div>
  );
}

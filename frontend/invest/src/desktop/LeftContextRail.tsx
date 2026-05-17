import type { Account } from "../types/invest";
import { accountSourceMeta, displayNameWithSource } from "./AccountSourceMeta";
import type { AssetCategoryKey } from "../types/filters";

interface ItemProps {
  label: string;
  toneDot?: string;
  badge?: string;
  value?: string;
  active?: boolean;
  onClick?: () => void;
}

function Item({ label, toneDot, badge, value, active, onClick }: ItemProps) {
  return (
    <button
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "9px 10px",
        borderRadius: 10,
        border: "none",
        background: active ? "var(--surface-2)" : "transparent",
        color: "var(--fg-1)",
        fontFamily: "inherit",
        fontSize: 13,
        fontWeight: 600,
        textAlign: "left",
        cursor: "pointer",
        whiteSpace: "nowrap",
        width: "100%",
      }}
    >
      {toneDot ? (
        <span
          aria-hidden
          style={{ width: 6, height: 6, borderRadius: 999, background: toneDot, flexShrink: 0 }}
        />
      ) : (
        <span aria-hidden style={{ width: 6, height: 6, borderRadius: 2, background: "var(--fg-2)", flexShrink: 0 }} />
      )}
      <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
      {badge != null && (
        <span style={{ fontSize: 10, color: "var(--fg-3)", fontWeight: 700, flexShrink: 0 }}>{badge}</span>
      )}
      {value != null && (
        <span style={{ fontSize: 11, color: "var(--fg-3)", fontFeatureSettings: '"tnum"', fontWeight: 500 }}>{value}</span>
      )}
    </button>
  );
}

const SectionLabel = ({ children }: { children: React.ReactNode }) => (
  <div
    style={{
      fontSize: 11,
      fontWeight: 700,
      color: "var(--fg-3)",
      letterSpacing: "0.06em",
      padding: "0 10px 8px",
      textTransform: "none",
    }}
  >
    {children}
  </div>
);

function fmtMillion(v: number | null | undefined): string {
  if (v == null) return "—";
  return `₩${(v / 1e6).toFixed(1)}M`;
}

const CATEGORIES: { key: AssetCategoryKey; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr_stock", label: "한국주식" },
  { key: "us_stock", label: "해외주식" },
  { key: "crypto", label: "코인" },
];

export type AccountFilterKey = "all" | string;

export function LeftContextRail({
  accounts,
  totalKrw,
  account,
  onAccount,
  category,
  onCategory,
}: {
  accounts: Account[];
  totalKrw: number;
  account: AccountFilterKey;
  onAccount: (k: AccountFilterKey) => void;
  category: AssetCategoryKey;
  onCategory: (c: AssetCategoryKey) => void;
}) {
  return (
    <aside data-testid="left-context-rail" style={{ display: "flex", flexDirection: "column", gap: 18, paddingTop: 4 }}>
      <div>
        <SectionLabel>계좌별 보기</SectionLabel>
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          <Item
            label="전체"
            value={fmtMillion(totalKrw)}
            active={account === "all"}
            onClick={() => onAccount("all")}
          />
          {accounts.map((a) => {
            const meta = accountSourceMeta(a.source);
            return (
              <Item
                key={a.accountId}
                label={displayNameWithSource(a)}
                badge={meta.badge}
                toneDot={`var(--pill-${meta.tone}-fg)`}
                value={fmtMillion(a.valueKrw)}
                active={account === a.source}
                onClick={() => onAccount(a.source)}
              />
            );
          })}
        </div>
      </div>

      <div>
        <SectionLabel>카테고리</SectionLabel>
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {CATEGORIES.map((c) => (
            <Item
              key={c.key}
              label={c.label}
              active={category === c.key}
              onClick={() => onCategory(c.key)}
            />
          ))}
        </div>
      </div>
    </aside>
  );
}

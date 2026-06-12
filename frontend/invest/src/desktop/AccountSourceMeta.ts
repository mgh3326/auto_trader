import type { Account, AccountSource, PriceState } from "../types/invest";
import type { PillTone } from "../ds";
import { pillToneForSource } from "./AccountSourceTone";

export interface AccountSourceMeta {
  label: string;
  shortLabel: string;
  badge: string;
  kindLabel: string;
  tone: PillTone;
}

const SOURCE_META: Record<AccountSource, AccountSourceMeta> = {
  kis: {
    label: "KIS 실계좌",
    shortLabel: "KIS",
    badge: "Live",
    kindLabel: "실계좌",
    tone: "kis",
  },
  upbit: {
    label: "Upbit",
    shortLabel: "Upbit",
    badge: "Crypto",
    kindLabel: "코인",
    tone: "upbit",
  },
  toss_manual: {
    label: "Toss 수동",
    shortLabel: "Toss",
    badge: "Manual",
    kindLabel: "수동",
    tone: "toss",
  },
  toss_api: {
    label: "Toss",
    shortLabel: "Toss",
    badge: "Live",
    kindLabel: "실계좌",
    tone: "toss",
  },
  pension_manual: {
    label: "연금 수동",
    shortLabel: "연금",
    badge: "Manual",
    kindLabel: "수동",
    tone: "pension",
  },
  isa_manual: {
    label: "ISA 수동",
    shortLabel: "ISA",
    badge: "Manual",
    kindLabel: "수동",
    tone: "isa",
  },
  kis_mock: {
    label: "KIS 모의",
    shortLabel: "KIS 모의",
    badge: "Mock",
    kindLabel: "모의",
    tone: "paper",
  },
  kiwoom_mock: {
    label: "Kiwoom 모의",
    shortLabel: "Kiwoom 모의",
    badge: "Mock",
    kindLabel: "모의",
    tone: "paper",
  },
  alpaca_paper: {
    label: "Alpaca Paper",
    shortLabel: "Alpaca Paper",
    badge: "Paper",
    kindLabel: "Paper",
    tone: "paper",
  },
  db_simulated: {
    label: "DB Paper",
    shortLabel: "DB Paper",
    badge: "Paper",
    kindLabel: "Paper",
    tone: "paper",
  },
};

export function accountSourceMeta(source: AccountSource): AccountSourceMeta {
  return SOURCE_META[source] ?? {
    label: source,
    shortLabel: source,
    badge: "Account",
    kindLabel: "계좌",
    tone: pillToneForSource(source),
  };
}

export function displayNameWithSource(account: Pick<Account, "displayName" | "source">): string {
  const meta = accountSourceMeta(account.source);
  const name = account.displayName.trim();
  if (!name) return meta.label;
  if (name.includes(meta.label) || name.includes(meta.shortLabel) || name.includes(meta.kindLabel)) return name;
  return `${name} · ${meta.label}`;
}

export function quoteFreshnessLabel(state: PriceState): string {
  if (state === "live") return "브로커/실시간";
  if (state === "stale") return "시세 지연";
  return "시세 없음";
}

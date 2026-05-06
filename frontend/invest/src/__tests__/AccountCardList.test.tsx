import { render, screen } from "@testing-library/react";
import { AccountCardList } from "../components/AccountCardList";
import type { Account } from "../types/invest";
import { expect, test } from "vitest";

const acct = (overrides: Partial<Account> = {}): Account => ({
  accountId: "a1",
  displayName: "KIS 실계좌",
  source: "kis",
  accountKind: "live",
  includedInHome: true,
  valueKrw: 31_420_000,
  costBasisKrw: 30_478_000,
  pnlKrw: 942_000,
  pnlRate: 0.031,
  cashBalances: { krw: 92_408, usd: 49.25 },
  buyingPower: { krw: 92_408, usd: 49.25 },
  ...overrides,
});

test("KIS card shows USD values and no badge", () => {
  render(<AccountCardList accounts={[acct()]} />);
  expect(screen.queryByText(/^live$/i)).toBeNull();
  expect(screen.getByText(/달러 · 현금/)).toBeInTheDocument();
});

test("KIS card shows '확인 필요' when USD warning present", () => {
  render(
    <AccountCardList
      accounts={[acct()]}
      warnings={[{ source: "kis", message: "USD 예수금 확인 불가" }]}
    />
  );
  expect(screen.getAllByText("확인 필요").length).toBeGreaterThan(0);
});

test("Upbit card shows KRW cash + buying power", () => {
  render(
    <AccountCardList
      accounts={[
        acct({
          accountId: "u1",
          source: "upbit",
          displayName: "Upbit",
          cashBalances: { krw: 412_000 },
          buyingPower: { krw: 412_000 },
        }),
      ]}
    />
  );
  expect(screen.getByText(/원화 · 현금/)).toBeInTheDocument();
  expect(screen.getByText(/원화 · 매수 가능/)).toBeInTheDocument();
});

test("Toss manual card shows quiet 수동 badge and falls back to '-' when empty", () => {
  render(
    <AccountCardList
      accounts={[
        acct({
          accountId: "t1",
          source: "toss_manual",
          accountKind: "manual",
          displayName: "Toss",
          costBasisKrw: null,
          pnlKrw: null,
          pnlRate: null,
          cashBalances: {},
          buyingPower: {},
        }),
      ]}
    />
  );
  expect(screen.getByText("수동")).toBeInTheDocument();
  expect(screen.getAllByText("-").length).toBeGreaterThan(0);
});

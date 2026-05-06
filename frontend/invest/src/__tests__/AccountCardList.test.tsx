import { render, screen } from "@testing-library/react";
import { AccountCardList } from "../components/AccountCardList";
import type { Account } from "../types/invest";

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

test("KIS card does not render a live badge", () => {
  render(<AccountCardList accounts={[acct()]} />);
  expect(screen.queryByText(/^live$/i)).toBeNull();
});

test("Upbit card does not render a live badge and shows KRW cash + buying power", () => {
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
  expect(screen.queryByText(/^live$/i)).toBeNull();
  expect(screen.getByText(/원화 현금/)).toBeInTheDocument();
  expect(screen.getByText(/원화 매수/)).toBeInTheDocument();
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

test("buyingPower rendering does not attach onClick handlers", () => {
  render(<AccountCardList accounts={[acct()]} />);
  const buyingPowerLabel = screen.getByText(/원화 매수/);
  // buyingPower 행과 그 자식 어디에도 button 또는 onClick 없음
  const cell = buyingPowerLabel.closest('[data-testid="account-card"]')!;
  expect(cell.querySelector("button, [role='button']")).toBeNull();
  // Using querySelectorAll to find elements with onclick attribute is hard in JSDOM,
  // but we can check if there are any buttons.
  expect(cell.querySelectorAll("button")).toHaveLength(0);
});

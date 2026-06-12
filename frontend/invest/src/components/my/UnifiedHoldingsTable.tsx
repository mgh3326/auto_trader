import { Link } from "react-router-dom";
import { Pill } from "../../ds";
import { pillToneForSource } from "../../desktop/AccountSourceTone";
import { stockDetailPath } from "../../stockDetailPath";
import type {
  Account,
  AccountSource,
  GroupedHolding,
  GroupedSourceBreakdown,
  PriceState,
} from "../../types/invest";

const COLS = "minmax(180px,1.7fr) 105px 118px 128px 118px minmax(210px,1.25fr)";

function fmtKrw(v: number | null | undefined): string {
  if (v == null) return "—";
  return `₩${Math.round(v).toLocaleString("ko-KR")}`;
}

function fmtUsd(v: number | null | undefined): string {
  if (v == null) return "—";
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtMoney(v: number | null | undefined, currency: GroupedHolding["currency"]): string {
  return currency === "USD" ? fmtUsd(v) : fmtKrw(v);
}

function fmtQty(qty: number, assetType: GroupedHolding["assetType"]): string {
  if (assetType === "crypto") return qty.toLocaleString("ko-KR", { maximumFractionDigits: 8 });
  return `${qty.toLocaleString("ko-KR")}주`;
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
}

function plColor(rate: number | null | undefined): string {
  if (rate == null) return "var(--fg-3)";
  return rate >= 0 ? "var(--gain)" : "var(--loss)";
}

const PRICE_STATE_LABEL: Record<PriceState, string> = {
  live: "실시간",
  stale: "시세 지연",
  missing: "시세 없음",
};

function priceStateTone(priceState: PriceState): "accent" | "warn" | "paper" {
  if (priceState === "live") return "accent";
  if (priceState === "stale") return "warn";
  return "paper";
}

function accountName(accounts: Account[], accountId: string): string | null {
  return accounts.find((account) => account.accountId === accountId)?.displayName ?? null;
}

const SOURCE_LABEL: Record<AccountSource, string> = {
  kis: "KIS",
  upbit: "Upbit",
  toss_manual: "Toss 수동",
  toss_api: "Toss",
  pension_manual: "퇴직연금",
  isa_manual: "ISA",
  kis_mock: "KIS 모의",
  kiwoom_mock: "키움 모의",
  alpaca_paper: "Alpaca",
  db_simulated: "DB 시뮬",
};

function sourceLabel(accounts: Account[], source: AccountSource): string {
  return accounts.find((account) => account.source === source)?.displayName ?? SOURCE_LABEL[source];
}

function SourceChip({ source, accounts }: { source: AccountSource; accounts: Account[] }) {
  return (
    <Pill tone={pillToneForSource(source)} size="sm">
      {sourceLabel(accounts, source)}
    </Pill>
  );
}

function QuantityCell({ holding }: { holding: GroupedHolding }) {
  const tradeable = holding.tradeableQuantity ?? holding.totalQuantity;
  const sellable = holding.sellableQuantity ?? tradeable;
  const pendingSell = holding.pendingSellQuantity ?? 0;
  const reference = holding.referenceQuantity ?? 0;

  return (
    <div style={{ textAlign: "right", color: "var(--fg-1)", fontWeight: 600 }}>
      <div>{fmtQty(holding.totalQuantity, holding.assetType)}</div>
      <div style={{ marginTop: 3, fontSize: 11, color: "var(--fg-3)", fontWeight: 500 }}>
        매매가능 {fmtQty(tradeable, holding.assetType)} · 매도가능 {fmtQty(sellable, holding.assetType)}
      </div>
      {pendingSell > 0 && (
        <div style={{ marginTop: 2, fontSize: 11, color: "var(--warn)", fontWeight: 500 }}>
          주문대기 {fmtQty(pendingSell, holding.assetType)}
        </div>
      )}
      {reference > 0 && (
        <div style={{ marginTop: 2, fontSize: 11, color: "var(--fg-3)", fontWeight: 500 }}>
          참고전용 {fmtQty(reference, holding.assetType)}
        </div>
      )}
    </div>
  );
}

function BreakdownLine({
  item,
  accounts,
  currency,
  assetType,
}: {
  item: GroupedSourceBreakdown;
  accounts: Account[];
  currency: GroupedHolding["currency"];
  assetType: GroupedHolding["assetType"];
}) {
  const name = accountName(accounts, item.accountId) ?? sourceLabel(accounts, item.source);
  const sellable = item.sellableQuantity ?? (item.isTradeable ? item.quantity : 0);
  const reference = item.referenceQuantity ?? (item.manualOnly ? item.quantity : 0);
  const metaLabel = item.manualOnly
    ? `참고전용 ${fmtQty(reference, assetType)}`
    : `매도가능 ${fmtQty(sellable, assetType)}`;
  return (
    <div
      data-testid="unified-holding-source-breakdown"
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(74px,1fr) auto auto",
        gap: 6,
        alignItems: "center",
        color: "var(--fg-2)",
        fontSize: 11,
      }}
    >
      <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</span>
      <span style={{ color: "var(--fg-3)", fontFeatureSettings: '"tnum"' }}>
        {fmtQty(item.quantity, assetType)} · {metaLabel}
      </span>
      <span style={{ color: plColor(item.pnlRate), fontWeight: 700, fontFeatureSettings: '"tnum"' }}>
        {fmtMoney(item.valueNative ?? item.valueKrw, currency)} · {fmtPct(item.pnlRate)}
      </span>
    </div>
  );
}

export function UnifiedHoldingsTable({
  holdings,
  accounts,
}: {
  holdings: GroupedHolding[];
  accounts: Account[];
}) {
  return (
    <div
      data-testid="unified-holdings-table"
      style={{
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 18,
        boxShadow: "var(--shadow-1)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: COLS,
          gap: 14,
          padding: "12px 20px",
          fontSize: 12,
          fontWeight: 700,
          color: "var(--fg-3)",
          borderBottom: "1px solid var(--divider)",
          background: "var(--surface-2)",
        }}
      >
        <div>종목</div>
        <div style={{ textAlign: "right" }}>보유수량</div>
        <div style={{ textAlign: "right" }}>평균단가</div>
        <div style={{ textAlign: "right" }}>평가금액</div>
        <div style={{ textAlign: "right" }}>손익률</div>
        <div>출처/계좌</div>
      </div>

      {holdings.length === 0 ? (
        <div data-testid="unified-holdings-empty" style={{ padding: 32, textAlign: "center", color: "var(--fg-3)", fontSize: 13 }}>
          표시할 보유 종목이 없습니다. 계좌 연동 또는 수동 보유 데이터 상태를 확인해 주세요.
        </div>
      ) : (
        holdings.map((holding, index) => {
          const href = stockDetailPath(holding.market, holding.symbol);
          const value = holding.valueNative ?? holding.valueKrw;
          const rowStyle = {
            display: "grid",
            gridTemplateColumns: COLS,
            gap: 14,
            alignItems: "start",
            padding: "16px 20px",
            borderTop: index === 0 ? "none" : "1px solid var(--divider)",
            color: "inherit",
            textDecoration: "none",
            fontFeatureSettings: '"tnum"',
          };
          const sourceList = holding.sourceBreakdown.length > 0
            ? [...new Set(holding.sourceBreakdown.map((source) => source.source))]
            : holding.includedSources;
          const content = (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                <div
                  aria-hidden
                  style={{
                    width: 36,
                    height: 36,
                    borderRadius: 10,
                    flexShrink: 0,
                    background: `var(--pill-${pillToneForSource(sourceList[0] ?? "db_simulated")}-bg)`,
                    color: `var(--pill-${pillToneForSource(sourceList[0] ?? "db_simulated")}-fg)`,
                    display: "grid",
                    placeItems: "center",
                    fontWeight: 800,
                    fontSize: 13,
                  }}
                >
                  {holding.displayName.slice(0, 1)}
                </div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: "flex", gap: 6, alignItems: "center", minWidth: 0 }}>
                    <strong style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--fg)" }}>
                      {holding.displayName}
                    </strong>
                    <Pill tone={priceStateTone(holding.priceState)} size="sm">
                      {PRICE_STATE_LABEL[holding.priceState]}
                    </Pill>
                  </div>
                  <div style={{ marginTop: 3, fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--font-mono)" }}>
                    {holding.market} · {holding.symbol}
                  </div>
                </div>
              </div>

              <QuantityCell holding={holding} />
              <div style={{ textAlign: "right", color: "var(--fg-2)", fontWeight: 600 }}>
                {fmtMoney(holding.averageCost, holding.currency)}
                {holding.currency === "USD" && <span style={{ fontSize: 10, color: "var(--fg-3)", marginLeft: 4 }}>USD</span>}
              </div>
              <div style={{ textAlign: "right" }}>
                <div style={{ color: "var(--fg)", fontWeight: 800 }}>{fmtMoney(value, holding.currency)}</div>
                <div style={{ color: "var(--fg-3)", fontSize: 11 }}>{holding.currency}</div>
              </div>
              <div style={{ textAlign: "right", color: plColor(holding.pnlRate), fontWeight: 800 }}>
                {fmtPct(holding.pnlRate)}
                {holding.pnlKrw != null && <div style={{ color: plColor(holding.pnlRate), fontSize: 11 }}>{fmtKrw(holding.pnlKrw)}</div>}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                  {sourceList.map((source) => (
                    <SourceChip key={source} source={source} accounts={accounts} />
                  ))}
                </div>
                {holding.sourceBreakdown.length > 0 ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    {holding.sourceBreakdown.map((item) => (
                      <BreakdownLine
                        key={item.holdingId}
                        item={item}
                        accounts={accounts}
                        currency={holding.currency}
                        assetType={holding.assetType}
                      />
                    ))}
                  </div>
                ) : (
                  <span style={{ color: "var(--fg-3)", fontSize: 11 }}>계좌별 상세 없음</span>
                )}
              </div>
            </>
          );

          return href ? (
            <Link key={holding.groupId} to={href} data-testid="unified-holding-row" style={rowStyle}>
              {content}
            </Link>
          ) : (
            <div key={holding.groupId} data-testid="unified-holding-row" style={rowStyle}>
              {content}
            </div>
          );
        })
      )}
    </div>
  );
}

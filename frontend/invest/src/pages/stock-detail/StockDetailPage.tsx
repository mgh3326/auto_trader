import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { DesktopShell } from "../../desktop/DesktopShell";
import { Button, Card, Hairline, Krw, PL, Pill, Sparkline, Usd } from "../../ds";
import {
  fetchStockDetail,
  fetchStockDetailCandles,
  fetchStockDetailNews,
  fetchStockDetailOrderLedger,
  fetchStockDetailOrders,
  fetchStockDetailResearchConsensus,
} from "../../api/stockDetail";
import { InvestorFlowCard } from "../../desktop/stock-detail/InvestorFlowCard";
import { OrderLedgerCard } from "../../desktop/stock-detail/OrderLedgerCard";
import type { LinkedOrder } from "../../types/investmentReports";
import type {
  StockDetailCandlesResponse,
  StockDetailFxSensitivity,
  StockDetailMarket,
  StockDetailNewsResponse,
  StockDetailOrdersResponse,
  StockDetailResearchConsensusResponse,
  StockDetailResponse,
} from "../../types/stockDetail";

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "−";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function fmtQty(v: number): string {
  return `${v.toLocaleString("ko-KR", { maximumFractionDigits: 6 })}주`;
}

function fmtKrwSigned(v: number | null | undefined): string {
  if (v == null) return "−";
  const rounded = Math.round(v);
  let sign = "";
  if (rounded > 0) sign = "+";
  if (rounded < 0) sign = "−";
  return `${sign}₩${Math.abs(rounded).toLocaleString("ko-KR")}`;
}

function fmtRate(v: number | null | undefined): string {
  if (v == null) return "−";
  return v.toLocaleString("ko-KR", { maximumFractionDigits: 2 });
}

function marketLabel(market: StockDetailMarket): string {
  return market.toUpperCase();
}

function currencyValue(currency: string, value: number | null | undefined) {
  if (currency === "USD") return <Usd v={value} size={32} weight={800} />;
  return <Krw v={value} size={32} weight={800} />;
}

function orderbookMessage(data: StockDetailResponse): string {
  if (data.orderbookSupport.supported && data.orderbook) return "호가를 표시합니다";
  if (data.orderbookSupport.reason === "us_unsupported") return "US 호가는 아직 지원하지 않습니다";
  if (data.orderbookSupport.reason === "provider_unavailable") return "호가 제공자 데이터를 사용할 수 없습니다";
  if (data.orderbookSupport.reason === "crypto_deferred") return "크립토 호가는 다음 단계에서 연결합니다";
  return "호가 데이터를 사용할 수 없습니다";
}

function HeaderCard({ data }: { data: StockDetailResponse }) {
  const quote = data.quote;
  return (
    <Card data-testid="stock-detail-header">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "flex-start" }}>
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <Pill tone={data.market === "us" ? "accent" : data.market === "kr" ? "kis" : "upbit"}>{marketLabel(data.market)}</Pill>
            <span style={{ color: "var(--fg-3)", fontSize: 13 }}>{data.instrumentType}</span>
          </div>
          <h1 style={{ margin: 0, fontSize: 28, letterSpacing: "-0.03em" }}>{data.displayName}</h1>
          <p style={{ margin: "6px 0 0", color: "var(--fg-3)", fontSize: 13 }}>
            {data.symbol} · {marketLabel(data.market)} · {data.exchange}
          </p>
        </div>
        <div style={{ textAlign: "right" }}>
          <div>{currencyValue(data.currency, quote?.price)}</div>
          <div style={{ marginTop: 6, color: (quote?.changeRate ?? 0) >= 0 ? "var(--gain)" : "var(--loss)", fontWeight: 700 }}>
            {fmtPct(quote?.changeRate)}
          </div>
        </div>
      </div>
    </Card>
  );
}

function TradeGuardrail({ data }: { data: StockDetailResponse }) {
  return (
    <Card data-testid="stock-detail-trade-guardrail" soft>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
        <div>
          <strong>주문 기능 준비중</strong>
          <p style={{ margin: "4px 0 0", color: "var(--fg-3)", fontSize: 12 }}>
            이 화면은 읽기 전용입니다. 실행 사유: {data.capabilities.execution.reason}
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Button disabled aria-label="매수 준비중">매수 준비중</Button>
          <Button disabled variant="secondary" aria-label="매도 준비중">매도 준비중</Button>
        </div>
      </div>
    </Card>
  );
}

function HoldingCard({ data }: { data: StockDetailResponse }) {
  const holding = data.holding;
  return (
    <Card data-testid="stock-detail-holding">
      <h2 style={{ margin: "0 0 12px", fontSize: 16 }}>내 보유</h2>
      {holding ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12 }}>
          <Metric label="수량" value={fmtQty(holding.totalQuantity)} />
          <Metric label="평단" value={data.currency === "USD" ? `$${holding.averageCost?.toFixed(2) ?? "−"}` : `₩${holding.averageCost?.toLocaleString("ko-KR") ?? "−"}`} />
          <Metric label="평가금액" value={holding.valueKrw == null ? "−" : `₩${Math.round(holding.valueKrw).toLocaleString("ko-KR")}`} />
          <div>
            <div style={{ color: "var(--fg-3)", fontSize: 12 }}>손익</div>
            <PL value={holding.pnlKrw ?? 0} pct={holding.pnlRate ?? 0} />
          </div>
        </div>
      ) : (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>보유 수량이 없습니다.</p>
      )}
    </Card>
  );
}

function FxSensitivityCard({ data }: Readonly<{ data: StockDetailFxSensitivity | null }>) {
  if (!data) return null;
  const isAvailable = data.status === "available";
  const basisLabel = data.basis === "portfolio_value" ? "보유 평가금액 기준" : "가정 기준";
  return (
    <Card data-testid="stock-detail-fx-sensitivity" soft>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
        <div>
          <h2 style={{ margin: "0 0 6px", fontSize: 16 }}>환율 민감도</h2>
          <p style={{ margin: 0, color: "var(--fg-3)", fontSize: 12 }}>
            {isAvailable ? `${data.currencyPair} ${fmtRate(data.baseFxRate)} · ${basisLabel}` : data.caution}
          </p>
        </div>
        <Pill tone={isAvailable ? "accent" : "paper"}>가정</Pill>
      </div>
      {isAvailable ? (
        <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 10 }}>
          {data.scenarios.map((scenario) => (
            <div key={scenario.label} style={{ border: "1px solid var(--line)", borderRadius: 14, padding: 12 }}>
              <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{scenario.label}</div>
              <div style={{ marginTop: 4, fontWeight: 800, color: (scenario.estimatedKrwImpact ?? 0) >= 0 ? "var(--gain)" : "var(--loss)" }}>
                {fmtKrwSigned(scenario.estimatedKrwImpact)}
              </div>
              <div style={{ marginTop: 4, color: "var(--fg-3)", fontSize: 12 }}>
                추정 평가액 {scenario.estimatedValueKrw == null ? "−" : `₩${Math.round(scenario.estimatedValueKrw).toLocaleString("ko-KR")}`}
              </div>
            </div>
          ))}
        </div>
      ) : null}
      {isAvailable ? <p style={{ margin: "10px 0 0", color: "var(--fg-3)", fontSize: 12 }}>{data.caution}</p> : null}
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{label}</div>
      <div style={{ marginTop: 4, fontWeight: 700 }}>{value}</div>
    </div>
  );
}

function ChartCard({ candles }: { candles: StockDetailCandlesResponse | undefined }) {
  const points = candles?.candles.map((c) => c.close) ?? [];
  return (
    <Card data-testid="stock-detail-chart">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={{ margin: 0, fontSize: 16 }}>차트</h2>
        <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{candles ? `${candles.candles.length}개 캔들 · ${candles.source}` : "불러오는 중"}</span>
      </div>
      <div style={{ marginTop: 16 }}>
        <Sparkline points={points} color="var(--accent)" height={96} width={560} />
      </div>
    </Card>
  );
}

function OrderbookCard({ data }: { data: StockDetailResponse }) {
  return (
    <Card data-testid="stock-detail-orderbook">
      <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>호가</h2>
      <p style={{ margin: 0, color: "var(--fg-3)" }}>{orderbookMessage(data)}</p>
    </Card>
  );
}

function OrdersCard({ orders }: { orders: StockDetailOrdersResponse | undefined }) {
  const empty = orders?.meta.emptyState === "no_filled_orders" || orders?.items.length === 0;
  return (
    <Card data-testid="stock-detail-orders">
      <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>체결 내역</h2>
      {!orders ? <p style={{ margin: 0, color: "var(--fg-3)" }}>불러오는 중입니다…</p> : null}
      {orders && empty ? <p style={{ margin: 0, color: "var(--fg-3)" }}>체결 내역이 없습니다.</p> : null}
      {orders && !empty ? <ul>{orders.items.map((o) => <li key={o.orderId ?? `${o.side}-${o.filledAt}`}>{o.side} {o.quantity}</li>)}</ul> : null}
    </Card>
  );
}

function CryptoDetailCard({ data }: { data: StockDetailResponse }) {
  const crypto = data.cryptoDetail;
  if (!crypto) return null;
  const pendingCount = crypto.pendingOrders.items.length;
  const recent = crypto.recentTrades.items.slice(0, 3);
  return (
    <Card data-testid="stock-detail-crypto-detail" soft>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
        <div>
          <h2 style={{ margin: "0 0 6px", fontSize: 16 }}>크립토 사전 체크</h2>
          <p style={{ margin: 0, color: "var(--fg-3)", fontSize: 12 }}>
            {crypto.profile.baseSymbol} · 미체결 {pendingCount}건 · {crypto.preOrderChecklist.mode}
          </p>
        </div>
        <Pill tone="upbit">read-only</Pill>
      </div>
      <ul style={{ margin: "12px 0 0", paddingLeft: 18, color: "var(--fg-2)", fontSize: 13 }}>
        {crypto.preOrderChecklist.items.slice(0, 6).map((item) => (
          <li key={item.key}><strong>{item.label}</strong> · {item.detail}</li>
        ))}
      </ul>
      <p style={{ margin: "10px 0 0", color: "var(--fg-3)", fontSize: 12 }}>{crypto.preOrderChecklist.disclaimer}</p>
      {recent.length > 0 ? (
        <div style={{ marginTop: 12 }}>
          <div style={{ color: "var(--fg-3)", fontSize: 12, marginBottom: 6 }}>최근 체결</div>
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--fg-2)", fontSize: 13 }}>
            {recent.map((trade) => <li key={`${trade.sequentialId ?? trade.tradeTime}-${trade.priceKrw}`}>₩{Math.round(trade.priceKrw).toLocaleString("ko-KR")} · {trade.volume.toLocaleString("ko-KR", { maximumFractionDigits: 6 })}</li>)}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

function ProfileCard({ data }: { data: StockDetailResponse }) {
  return (
    <Card data-testid="stock-detail-profile">
      <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>프로필</h2>
      <div style={{ color: "var(--fg-2)", fontSize: 13 }}>{data.instrumentType} · {data.currency} · {data.assetCategory}</div>
      {data.valuation ? (
        <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
          <Metric label="52주 고가" value={data.valuation.high52w?.toLocaleString("en-US") ?? "−"} />
          <Metric label="52주 저가" value={data.valuation.low52w?.toLocaleString("en-US") ?? "−"} />
          <Metric label="배당" value={fmtPct(data.valuation.dividendYield)} />
        </div>
      ) : null}
    </Card>
  );
}

function ResearchConsensusCard({ data, error }: { data: StockDetailResearchConsensusResponse | undefined; error: string | undefined }) {
  const consensus = data?.consensus;
  const stateLabel = data?.dataState === "stale" ? "오래된 데이터" : data?.state === "partial" ? "일부 데이터" : data?.state === "missing" ? "데이터 없음" : "최신";
  return (
    <Card data-testid="stock-detail-research-consensus">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
        <div>
          <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>리서치 · 컨센서스</h2>
          {!data && !error ? <p style={{ margin: 0, color: "var(--fg-3)" }}>리서치 데이터를 불러오는 중입니다…</p> : null}
          {error ? <p style={{ margin: 0, color: "var(--danger)" }}>리서치 데이터를 사용할 수 없습니다.</p> : null}
        </div>
        {data ? <Pill tone={data.dataState === "fresh" ? "accent" : data.dataState === "stale" ? "paper" : "loss"}>{stateLabel}</Pill> : null}
      </div>
      {data && consensus ? (
        <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 10 }}>
          <Metric label="Buy" value={`${consensus.buyCount}/${consensus.totalCount}`} />
          <Metric label="Hold" value={`${consensus.holdCount}`} />
          <Metric label="목표가 평균" value={consensus.avgTargetPrice == null ? "−" : Math.round(consensus.avgTargetPrice).toLocaleString("ko-KR")} />
          <Metric label="상승여력" value={fmtPct(consensus.upsidePct)} />
        </div>
      ) : null}
      {data && !consensus && !error ? (
        <p style={{ margin: "10px 0 0", color: "var(--fg-3)" }}>{data.emptyReason ? "애널리스트 컨센서스와 리서치 인용이 없습니다." : "컨센서스 없이 리서치 인용만 표시합니다."}</p>
      ) : null}
      {data && data.citations.length > 0 ? (
        <ul style={{ listStyle: "none", margin: "12px 0 0", padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {data.citations.slice(0, 3).map((citation, index) => (
            <li key={`${citation.source}-${citation.title ?? index}`}>
              <div style={{ fontWeight: 700 }}>{citation.title ?? "제목 없음"}</div>
              <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{citation.source}{citation.analyst ? ` · ${citation.analyst}` : ""}</div>
              {citation.excerpt ? <p style={{ margin: "4px 0 0", color: "var(--fg-2)", fontSize: 12 }}>{citation.excerpt}</p> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {data && data.warnings.length > 0 ? (
        <p style={{ margin: "10px 0 0", color: "var(--fg-3)", fontSize: 12 }}>경고: {data.warnings.join(", ")}</p>
      ) : null}
    </Card>
  );
}

function AnalysisCard({ data }: { data: StockDetailResponse }) {
  const analysis = data.latestAnalysis;
  return (
    <Card data-testid="stock-detail-analysis">
      <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>최근 분석</h2>
      {analysis ? (
        <>
          <Pill tone={analysis.decision === "buy" ? "gain" : analysis.decision === "sell" ? "loss" : "paper"}>{analysis.decision ?? "hold"}</Pill>
          <ul style={{ margin: "12px 0 0", paddingLeft: 18, color: "var(--fg-2)" }}>
            {analysis.reasonsTop3.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </>
      ) : (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>최근 분석이 없습니다.</p>
      )}
    </Card>
  );
}

function NaverPocCard({ data }: { data: StockDetailResponse }) {
  const poc = data.naverEnrichment;
  if (!poc) return null;
  const verified = poc.endpoints.filter((e) => e.status.startsWith("verified_200")).length;
  return (
    <Card data-testid="stock-detail-naver-poc" soft>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
        <div>
          <h2 style={{ margin: "0 0 6px", fontSize: 16 }}>Naver 원천 데이터 PoC</h2>
          <p style={{ margin: 0, color: "var(--fg-3)", fontSize: 12 }}>
            {poc.naverCode} · {verified}/{poc.endpoints.length} verified · live fetch {poc.liveFetchEnabled ? "on" : "off"}
          </p>
        </div>
        <Pill tone="paper">read-only</Pill>
      </div>
      <ul style={{ margin: "12px 0 0", paddingLeft: 18, color: "var(--fg-2)", fontSize: 13 }}>
        {poc.usefulFields.slice(0, 3).map((field) => <li key={field}>{field}</li>)}
      </ul>
      <p style={{ margin: "10px 0 0", color: "var(--fg-3)", fontSize: 12 }}>
        토론 본문·인증성 투자정보·스케줄 수집은 제외합니다. 상세 맵: {poc.docsPath}
      </p>
    </Card>
  );
}

function NewsCard({ news }: { news: StockDetailNewsResponse | undefined }) {
  return (
    <Card data-testid="stock-detail-news">
      <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>뉴스 · 공시</h2>
      {!news ? <p style={{ margin: 0, color: "var(--fg-3)" }}>불러오는 중입니다…</p> : null}
      {news && news.items.length === 0 ? <p style={{ margin: 0, color: "var(--fg-3)" }}>관련 뉴스가 없습니다.</p> : null}
      {news && news.items.length > 0 ? (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {news.items.slice(0, 5).map((item) => (
            <li key={item.id}>
              <a href={item.url} style={{ color: "var(--fg-1)", fontWeight: 700, textDecoration: "none" }}>{item.title}</a>
              <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{item.publisher ?? item.sourceMarket?.toUpperCase()}</div>
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

function MemoCard() {
  return (
    <Card data-testid="stock-detail-memo-placeholder" soft>
      <strong>메모</strong>
      <p style={{ margin: "6px 0 0", color: "var(--fg-3)", fontSize: 13 }}>
        투자 메모와 저널 연결 영역입니다. 외부 게시판은 이 MVP에 포함하지 않습니다.
      </p>
    </Card>
  );
}

export function StockDetailPage() {
  const params = useParams();
  const market = (params.market ?? "us") as StockDetailMarket;
  const symbol = (params.symbol ?? "").toUpperCase();
  const [data, setData] = useState<StockDetailResponse | undefined>();
  const [candles, setCandles] = useState<StockDetailCandlesResponse | undefined>();
  const [orders, setOrders] = useState<StockDetailOrdersResponse | undefined>();
  const [orderLedger, setOrderLedger] = useState<LinkedOrder[] | undefined>();
  const [news, setNews] = useState<StockDetailNewsResponse | undefined>();
  const [researchConsensus, setResearchConsensus] = useState<StockDetailResearchConsensusResponse | undefined>();
  const [researchErr, setResearchErr] = useState<string | undefined>();
  const [err, setErr] = useState<string | undefined>();

  useEffect(() => {
    let cancel = false;
    setData(undefined);
    setCandles(undefined);
    setOrders(undefined);
    setOrderLedger(undefined);
    setNews(undefined);
    setResearchConsensus(undefined);
    setResearchErr(undefined);
    setErr(undefined);

    fetchStockDetail({ market, symbol })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    if (market !== "crypto") {
      fetchStockDetailResearchConsensus({ market, symbol })
        .then((r) => !cancel && setResearchConsensus(r))
        .catch((e) => !cancel && setResearchErr(String(e?.message ?? e)));
    }
    fetchStockDetailCandles({ market, symbol, period: "1d" })
      .then((r) => !cancel && setCandles(r))
      .catch(() => undefined);
    fetchStockDetailOrders({ market, symbol })
      .then((r) => !cancel && setOrders(r))
      .catch(() => undefined);
    fetchStockDetailOrderLedger({ market, symbol })
      .then((r) => !cancel && setOrderLedger(r))
      .catch(() => !cancel && setOrderLedger([]));
    fetchStockDetailNews({ market, symbol, limit: 5 })
      .then((r) => !cancel && setNews(r))
      .catch(() => undefined);

    return () => {
      cancel = true;
    };
  }, [market, symbol]);

  const sideDetails = useMemo(() => {
    if (!data) {
      return <MemoCard />;
    }
    return (
      <div
        data-testid="stock-detail-side"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
          gap: 14,
        }}
      >
        <ProfileCard data={data} />
        {market !== "crypto" ? <ResearchConsensusCard data={researchConsensus} error={researchErr} /> : null}
        {market === "crypto" ? <CryptoDetailCard data={data} /> : null}
        <AnalysisCard data={data} />
        <NaverPocCard data={data} />
        <MemoCard />
      </div>
    );
  }, [data, market, researchConsensus, researchErr]);

  return (
    <DesktopShell
      center={
        <div data-testid="stock-detail-shell" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {err ? <Card><span style={{ color: "var(--danger)" }}>오류: {err}</span></Card> : null}
          {!data && !err ? <Card>종목 정보를 불러오는 중입니다…</Card> : null}
          {data ? (
            <>
              <HeaderCard data={data} />
              <TradeGuardrail data={data} />
              <HoldingCard data={data} />
              <FxSensitivityCard data={data.fxSensitivity} />
              <ChartCard candles={candles} />
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                <OrderbookCard data={data} />
                <OrdersCard orders={orders} />
              </div>
              <OrderLedgerCard orders={orderLedger} />
              <NewsCard news={news} />
              {data.market === "kr" && data.investorFlow ? (
                <InvestorFlowCard data={data.investorFlow} />
              ) : null}
              {data.meta.warnings.length > 0 ? (
                <Card soft>
                  {data.meta.warnings.map((w) => <div key={w}>{w}</div>)}
                </Card>
              ) : null}
              <Hairline />
              {sideDetails}
            </>
          ) : null}
        </div>
      }
    />
  );
}

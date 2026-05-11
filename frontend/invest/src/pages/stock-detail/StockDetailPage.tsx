import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { DesktopShell } from "../../desktop/DesktopShell";
import { Button, Card, Hairline, Krw, PL, Pill, Sparkline, Usd } from "../../ds";
import {
  fetchStockDetail,
  fetchStockDetailCandles,
  fetchStockDetailNews,
  fetchStockDetailOrders,
} from "../../api/stockDetail";
import type {
  StockDetailCandlesResponse,
  StockDetailMarket,
  StockDetailNewsResponse,
  StockDetailOrderBucket,
  StockDetailOrdersResponse,
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

function marketLabel(market: StockDetailMarket): string {
  return market.toUpperCase();
}

function currencyValue(currency: string, value: number | null | undefined) {
  if (currency === "USD") return <Usd v={value} size={32} weight={800} />;
  return <Krw v={value} size={32} weight={800} />;
}

function fmtMoney(currency: string, value: number | null | undefined): string {
  if (value == null) return "−";
  if (currency === "USD") return `$${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
  return `₩${Math.round(value).toLocaleString("ko-KR")}`;
}

function blockStateLabel(state: string | undefined): string {
  switch (state) {
    case "fresh": return "최신";
    case "stale": return "오래된 데이터";
    case "partial": return "일부 데이터";
    case "unsupported": return "미지원";
    case "error": return "조회 오류";
    case "provider_unwired": return "업데이트 대기";
    case "missing": return "데이터 없음";
    default: return "상태 미확인";
  }
}

function orderbookMessage(data: StockDetailResponse): string {
  if (data.orderbookSupport.supported && data.orderbook) return "호가를 표시합니다";
  if (data.orderbookSupport.reason === "us_unsupported") return "US 호가는 아직 지원하지 않습니다";
  if (data.orderbookSupport.reason === "crypto_deferred") return "크립토 호가는 다음 단계에서 연결합니다";
  return "호가 데이터를 사용할 수 없습니다";
}

function HeaderCard({ data }: { data: StockDetailResponse }) {
  const quote = data.quote;
  const quoteMissing = !quote || data.meta.blockStates.quote !== "fresh";
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
          {quoteMissing ? (
            <div style={{ marginTop: 4, color: "var(--fg-3)", fontSize: 12 }}>
              시세 없음 · {blockStateLabel(data.meta.blockStates.quote)}
            </div>
          ) : null}
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
        <div style={{ display: "grid", gap: 12 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12 }}>
            <Metric label="수량" value={fmtQty(holding.totalQuantity)} />
            <Metric label="평단" value={data.currency === "USD" ? `$${holding.averageCost?.toFixed(2) ?? "−"}` : `₩${holding.averageCost?.toLocaleString("ko-KR") ?? "−"}`} />
            <Metric label="평가금액" value={holding.valueKrw == null ? "−" : `₩${Math.round(holding.valueKrw).toLocaleString("ko-KR")}`} />
            <div>
              <div style={{ color: "var(--fg-3)", fontSize: 12 }}>손익</div>
              <PL value={holding.pnlKrw ?? 0} pct={holding.pnlRate ?? 0} />
            </div>
          </div>
          {holding.sourceBreakdown.length > 0 ? (
            <div style={{ display: "grid", gap: 8 }}>
              <div style={{ color: "var(--fg-3)", fontSize: 12, fontWeight: 700 }}>계좌별 보유</div>
              {holding.sourceBreakdown.map((source, idx) => (
                <div key={`${source.source}-${source.accountName ?? idx}`} style={{ border: "1px solid var(--border)", borderRadius: 12, padding: 10, display: "grid", gap: 4 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <strong>{source.accountName ?? source.source}</strong>
                    <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{source.source}</span>
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8, fontSize: 12 }}>
                    <span>수량 <strong>{fmtQty(source.quantity)}</strong></span>
                    <span>평균 단가 <strong>{fmtMoney(data.currency, source.averageCost)}</strong></span>
                    <span>평가 <strong>{fmtMoney(data.currency, source.valueNative ?? source.valueKrw)}</strong></span>
                  </div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>보유 수량이 없습니다.</p>
      )}
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
  const dataState = candles?.meta.dataState;
  const empty = !candles || candles.candles.length === 0 || dataState === "missing" || dataState === "provider_unwired";
  return (
    <Card data-testid="stock-detail-chart">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={{ margin: 0, fontSize: 16 }}>차트</h2>
        <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{candles ? `${candles.candles.length}개 캔들 · ${candles.source}` : "불러오는 중"}</span>
      </div>
      {empty ? (
        <div style={{ marginTop: 16, color: "var(--fg-3)" }}>
          <div>차트 데이터 없음</div>
          <div style={{ fontSize: 12, marginTop: 4 }}>
            {candles?.source ? `${candles.source} · ` : ""}{blockStateLabel(dataState)}
          </div>
        </div>
      ) : (
        <div style={{ marginTop: 16 }}>
          <Sparkline points={points} color="var(--accent)" height={96} width={560} />
        </div>
      )}
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
  const filled: StockDetailOrderBucket | undefined = orders?.filled ?? (orders ? {
    items: orders.items,
    nextCursor: orders.nextCursor,
    state: orders.items.length === 0 ? "empty" : "present",
    emptyState: orders.meta.emptyState,
    source: null,
    warnings: orders.meta.warnings,
  } : undefined);
  const pending = orders?.pending;
  const filledEmpty = filled?.emptyState === "no_filled_orders";
  const pendingEmpty = pending?.state === "empty" && pending.emptyState === "no_pending_orders" && pending.source !== null;
  return (
    <Card data-testid="stock-detail-orders">
      <div style={{ display: "grid", gap: 14 }}>
        <section data-testid="stock-detail-orders-filled">
          <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>체결 내역</h2>
          {!orders ? <p style={{ margin: 0, color: "var(--fg-3)" }}>불러오는 중입니다…</p> : null}
          {filled && filledEmpty ? <p style={{ margin: 0, color: "var(--fg-3)" }}>체결 내역이 없습니다.</p> : null}
          {filled && !filledEmpty ? <ul>{filled.items.map((o) => <li key={o.orderId ?? `${o.side}-${o.filledAt}`}>{o.side} {o.quantity}</li>)}</ul> : null}
        </section>
        <Hairline />
        <section data-testid="stock-detail-orders-pending">
          <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>대기 주문</h2>
          {!orders ? <p style={{ margin: 0, color: "var(--fg-3)" }}>불러오는 중입니다…</p> : null}
          {pending?.state === "provider_unwired" ? (
            <p style={{ margin: 0, color: "var(--fg-3)" }}>대기 주문 조회가 아직 연결되지 않았습니다.</p>
          ) : null}
          {pending?.state === "error" ? (
            <p style={{ margin: 0, color: "var(--fg-3)" }}>대기 주문 조회에 실패했습니다.</p>
          ) : null}
          {pending && pending.state !== "provider_unwired" && pending.state !== "error" && pendingEmpty ? (
            <p style={{ margin: 0, color: "var(--fg-3)" }}>대기중인 주문이 없어요</p>
          ) : null}
          {pending && !pendingEmpty && pending.state !== "provider_unwired" && pending.state !== "error" ? (
            <ul>{pending.items.map((o) => <li key={o.orderId ?? `${o.side}-${o.symbol}`}>{o.side} {o.quantity}</li>)}</ul>
          ) : null}
        </section>
      </div>
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
      ) : (
        <p style={{ margin: "12px 0 0", color: "var(--fg-3)" }}>밸류에이션 데이터 없음 · {blockStateLabel(data.meta.blockStates.valuation)}</p>
      )}
      {data.screenerSnapshot ? (
        <div style={{ marginTop: 12 }}><Metric label="스크리너" value={data.screenerSnapshot.source ?? data.screenerSnapshot.freshness} /></div>
      ) : (
        <p style={{ margin: "8px 0 0", color: "var(--fg-3)" }}>스크리너 스냅샷 없음 · {blockStateLabel(data.meta.blockStates.screenerSnapshot)}</p>
      )}
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
        <p style={{ margin: 0, color: "var(--fg-3)" }}>최근 분석 데이터 없음 · {blockStateLabel(data.meta.blockStates.latestAnalysis)}</p>
      )}
    </Card>
  );
}

function NewsCard({ news }: { news: StockDetailNewsResponse | undefined }) {
  return (
    <Card data-testid="stock-detail-news">
      <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>뉴스</h2>
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
  const [news, setNews] = useState<StockDetailNewsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();

  useEffect(() => {
    let cancel = false;
    setData(undefined);
    setCandles(undefined);
    setOrders(undefined);
    setNews(undefined);
    setErr(undefined);

    fetchStockDetail({ market, symbol })
      .then((r) => !cancel && setData(r))
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    fetchStockDetailCandles({ market, symbol, period: "1d" })
      .then((r) => !cancel && setCandles(r))
      .catch(() => undefined);
    fetchStockDetailOrders({ market, symbol })
      .then((r) => !cancel && setOrders(r))
      .catch(() => undefined);
    fetchStockDetailNews({ market, symbol, limit: 5 })
      .then((r) => !cancel && setNews(r))
      .catch(() => undefined);

    return () => {
      cancel = true;
    };
  }, [market, symbol]);

  const right = useMemo(() => (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {data ? <ProfileCard data={data} /> : null}
      {data ? <AnalysisCard data={data} /> : null}
      <MemoCard />
    </div>
  ), [data]);

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
              <ChartCard candles={candles} />
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                <OrderbookCard data={data} />
                <OrdersCard orders={orders} />
              </div>
              <NewsCard news={news} />
              {data.meta.warnings.length > 0 ? (
                <Card soft>
                  {data.meta.warnings.map((w) => <div key={w}>{w}</div>)}
                </Card>
              ) : null}
              <Hairline />
            </>
          ) : null}
        </div>
      }
      right={right}
    />
  );
}

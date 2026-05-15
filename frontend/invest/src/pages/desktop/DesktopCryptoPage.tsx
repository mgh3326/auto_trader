import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightRemotePanel } from "../../desktop/RightRemotePanel";
import { fetchCryptoDashboard, fetchCryptoNaverReference } from "../../api/investCrypto";
import type {
  CryptoCandidateInsight,
  CryptoDashboardResponse,
  CryptoMarketCard,
  CryptoRiskLevel,
  CryptoSourceState,
  NaverCryptoReferenceResponse,
} from "../../types/investCrypto";
import "../../desktop/screener/screener.css";

function formatKrw(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "-";
  return `${Math.round(value).toLocaleString("ko-KR")}원`;
}

function formatPct(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "-";
  return `${(value * 100).toFixed(2)}%`;
}

function sourceFreshness(fetchedAt: string | null | undefined): string {
  if (!fetchedAt) return "freshness unavailable";
  return `fetched ${fetchedAt}`;
}

function sourceSummary(sources: CryptoSourceState[], sourceIds: string[]): string {
  const selected = sources.filter((source) => sourceIds.includes(source.source));
  if (selected.length === 0) return "출처: 확인 불가";
  return `출처: ${selected.map((source) => `${source.label} ${source.state} (${sourceFreshness(source.fetchedAt)})`).join(" · ")}`;
}

function SourceLabels({ sources, sourceIds }: { sources: CryptoSourceState[]; sourceIds: string[] }) {
  const selected = sources.filter((source) => sourceIds.includes(source.source));
  if (selected.length === 0) {
    return <span className="screener-chip">출처 확인 불가</span>;
  }
  return (
    <>
      {selected.map((source) => (
        <span key={`${source.source}-${source.state}`} className="screener-chip">
          {source.label}: {source.state} · {sourceFreshness(source.fetchedAt)}
        </span>
      ))}
    </>
  );
}

function cardMetric(card: CryptoMarketCard): string {
  if (card.accTradePrice24h === null) return "거래대금 -";
  if (card.accTradePrice24h >= 1_0000_0000) {
    return `거래대금 ${(card.accTradePrice24h / 1_0000_0000).toFixed(1)}억`;
  }
  return `거래대금 ${Math.round(card.accTradePrice24h).toLocaleString("ko-KR")}`;
}

const riskLabels: Record<CryptoRiskLevel, string> = {
  high: "높음",
  medium: "중간",
  low: "낮음",
  unknown: "미확인",
};

const reasonLabels: Record<string, string> = {
  momentum: "변화",
  liquidity: "유동성",
  spread: "호가 안정",
  watched: "검토 목록",
  held: "보유",
  pending_order: "미체결",
  data_quality: "데이터 확인",
};

function riskCounts(cards: CryptoMarketCard[]): Record<CryptoRiskLevel, number> {
  return cards.reduce<Record<CryptoRiskLevel, number>>(
    (counts, card) => {
      const level = card.risk?.level ?? "unknown";
      counts[level] += 1;
      return counts;
    },
    { high: 0, medium: 0, low: 0, unknown: 0 },
  );
}

function CryptoCard({ card, sources }: { card: CryptoMarketCard; sources: CryptoSourceState[] }) {
  return (
    <Link
      to={`/crypto/${encodeURIComponent(card.symbol)}`}
      style={{
        display: "block",
        padding: 16,
        border: "1px solid var(--border)",
        borderRadius: 14,
        background: "var(--surface)",
        color: "inherit",
        textDecoration: "none",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <div>
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{card.symbol}</div>
          <h3 style={{ margin: "4px 0 0" }}>{card.displayName}</h3>
        </div>
        <div style={{ textAlign: "right" }}>
          <strong>{formatKrw(card.priceKrw)}</strong>
          <div style={{ color: (card.changeRate24h ?? 0) >= 0 ? "var(--danger)" : "var(--blue)", fontSize: 13 }}>
            {formatPct(card.changeRate24h)}
          </div>
        </div>
      </div>
      <div style={{ marginTop: 12, color: "var(--fg-3)", fontSize: 13 }}>
        {cardMetric(card)} · 호가 스프레드 {card.orderbookSpreadPct === null ? "-" : `${card.orderbookSpreadPct.toFixed(3)}%`}
      </div>
      <div style={{ marginTop: 8, color: "var(--fg-3)", fontSize: 12 }}>
        {sourceSummary(sources, ["upbit_ticker", "upbit_orderbook"])}
      </div>
      {card.risk && (
        <div style={{ marginTop: 8, color: "var(--fg-2)", fontSize: 13 }}>
          리스크 {riskLabels[card.risk.level]} · {card.risk.score}
          {card.risk.reasons.length > 0 && (
            <span style={{ color: "var(--fg-3)" }}> · {card.risk.reasons.slice(0, 2).join(" · ")}</span>
          )}
        </div>
      )}
      {card.badges.length > 0 && (
        <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 6 }}>
          {card.badges.map((badge, idx) => (
            <span key={`${badge.kind}-${idx}`} className="screener-chip">
              {badge.label}
            </span>
          ))}
        </div>
      )}
    </Link>
  );
}

function RiskSummary({ cards, sources }: { cards: CryptoMarketCard[]; sources: CryptoSourceState[] }) {
  const counts = riskCounts(cards);
  return (
    <section aria-label="리스크 요약" style={{ marginTop: 16, padding: 16, border: "1px solid var(--border)", borderRadius: 14 }}>
      <h2 style={{ marginTop: 0 }}>리스크 요약</h2>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {(["high", "medium", "low", "unknown"] as CryptoRiskLevel[]).map((level) => (
          <span key={level} className="screener-chip">
            {riskLabels[level]} {counts[level]}
          </span>
        ))}
      </div>
      <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 6 }}>
        <SourceLabels sources={sources} sourceIds={["upbit_ticker", "upbit_orderbook", "pending_orders", "mcp_risk_reference"]} />
      </div>
    </section>
  );
}

function CandidateInsightCard({ candidate }: { candidate: CryptoCandidateInsight }) {
  return (
    <article style={{ padding: 12, border: "1px solid var(--border)", borderRadius: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <strong>{candidate.rank}. {candidate.displayName}</strong>
        <span>점수 {candidate.score}</span>
      </div>
      <div style={{ marginTop: 4, color: "var(--fg-3)", fontSize: 13 }}>
        {candidate.symbol} · 리스크 {riskLabels[candidate.riskLevel]}
      </div>
      <p style={{ margin: "8px 0", color: "var(--fg-2)" }}>{candidate.summary}</p>
      {candidate.reasons.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {candidate.reasons.map((reason) => (
            <span key={reason} className="screener-chip">{reasonLabels[reason] ?? reason}</span>
          ))}
        </div>
      )}
    </article>
  );
}

function CandidateInsights({ candidates, sources }: { candidates: CryptoCandidateInsight[]; sources: CryptoSourceState[] }) {
  return (
    <section aria-label="후보 인사이트" style={{ marginTop: 18, padding: 16, border: "1px solid var(--border)", borderRadius: 14 }}>
      <h2 style={{ marginTop: 0 }}>후보 인사이트</h2>
      <p style={{ color: "var(--fg-3)" }}>후보 인사이트는 참고용이며 주문/감시 등록을 실행하지 않습니다.</p>
      <div style={{ marginBottom: 10, display: "flex", flexWrap: "wrap", gap: 6 }}>
        <SourceLabels sources={sources} sourceIds={["upbit_ticker", "upbit_orderbook", "pending_orders", "mcp_candidate_reference"]} />
      </div>
      {candidates.length === 0 ? (
        <p style={{ color: "var(--fg-3)" }}>조건에 맞는 후보 인사이트가 없습니다.</p>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {candidates.map((candidate) => (
            <CandidateInsightCard key={candidate.symbol} candidate={candidate} />
          ))}
        </div>
      )}
    </section>
  );
}

function cryptoErrorMessage(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error ?? "");
  if (/Failed to fetch|NetworkError|Load failed|crypto\/dashboard \d{3}/i.test(text)) {
    return "크립토 대시보드 데이터를 일시적으로 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }
  return text || "크립토 대시보드 데이터를 일시적으로 불러오지 못했습니다.";
}

function ReferencePanel({ reference }: { reference: NaverCryptoReferenceResponse }) {
  const kimchi = reference.kimchiPremium;
  return (
    <section style={{ marginTop: 18, padding: 16, border: "1px solid var(--border)", borderRadius: 14, background: "var(--surface)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "baseline" }}>
        <div>
          <h2 style={{ margin: 0 }}>Naver 참고 지표</h2>
          <p style={{ marginTop: 6, color: "var(--fg-3)" }}>출처 라벨이 있는 읽기 전용 참고 데이터입니다. 주문 실행 없음.</p>
        </div>
        <span className="screener-chip">{reference.capabilities.execution.state}</span>
      </div>
      {reference.warnings.length > 0 && (
        <div style={{ marginTop: 10, color: "var(--fg-3)", fontSize: 13 }}>
          {reference.warnings.slice(0, 3).join(" · ")}
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12, marginTop: 14 }}>
        <div>
          <strong>김치 프리미엄</strong>
          <div style={{ marginTop: 4 }}>{kimchi?.premiumPct === null || kimchi?.premiumPct === undefined ? "-" : `${kimchi.premiumPct.toFixed(2)}%`}</div>
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{kimchi?.caution ?? "참고용 매크로 지표"}</div>
        </div>
        <div>
          <strong>프로필</strong>
          <div style={{ marginTop: 4 }}>{reference.profile?.displayName ?? reference.symbol ?? "-"}</div>
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{reference.profile?.officialMarket ?? "UPBIT/KRW"} · reference-only</div>
        </div>
        <div>
          <strong>뉴스</strong>
          <div style={{ marginTop: 4 }}>{reference.news?.items.length ?? 0}건</div>
          <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{reference.news?.items[0]?.title ?? "최근 크립토 뉴스 없음"}</div>
        </div>
      </div>
      {reference.rank.length > 0 && (
        <div style={{ marginTop: 14, display: "grid", gap: 8 }}>
          {reference.rank.slice(0, 5).map((item) => (
            <div key={item.symbol} style={{ display: "flex", justifyContent: "space-between", gap: 12, color: "var(--fg-2)", fontSize: 13 }}>
              <span>#{item.rank} {item.displayName} <span style={{ color: "var(--fg-3)" }}>{item.source}</span></span>
              <span>{formatKrw(item.priceKrw)} · {formatPct(item.changeRate24h)}</span>
            </div>
          ))}
        </div>
      )}
      <div style={{ marginTop: 12, display: "flex", flexWrap: "wrap", gap: 6 }}>
        {reference.sources.map((source) => (
          <span key={`${source.source}-${source.state}`} className="screener-chip">
            {source.label}: {source.state} · {source.freshness} · {source.fetchedAt ?? "freshness unavailable"}
          </span>
        ))}
      </div>
    </section>
  );
}

function ReferenceFallback() {
  return (
    <section style={{ marginTop: 18, padding: 16, border: "1px solid var(--border)", borderRadius: 14, background: "var(--surface)" }}>
      <h2 style={{ margin: 0 }}>Naver 참고 지표</h2>
      <p style={{ color: "var(--fg-3)" }}>
        Naver/MCP 참고 지표를 불러오지 못했습니다. 대시보드 시세·리스크 카드는 계속 읽기 전용으로 표시됩니다.
      </p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        <span className="screener-chip">Naver crypto reference: unavailable · freshness unavailable</span>
        <span className="screener-chip">MCP kimchi premium: unavailable · freshness unavailable</span>
      </div>
    </section>
  );
}

export function DesktopCryptoPage() {
  const [data, setData] = useState<CryptoDashboardResponse | undefined>();
  const [reference, setReference] = useState<NaverCryptoReferenceResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();

  useEffect(() => {
    let cancel = false;
    setErr(undefined);
    Promise.allSettled([
      fetchCryptoDashboard({ limit: 20 }),
      fetchCryptoNaverReference({ symbol: "KRW-BTC", limit: 20 }),
    ]).then(([dashboardResult, referenceResult]) => {
      if (cancel) return;
      if (dashboardResult.status === "fulfilled") {
        setData(dashboardResult.value);
      } else {
        setErr(cryptoErrorMessage(dashboardResult.reason));
      }
      if (referenceResult.status === "fulfilled") {
        setReference(referenceResult.value);
      }
    });
    return () => { cancel = true; };
  }, []);

  return (
    <DesktopShell
      left={
        <aside style={{ padding: 16 }}>
          <h2 style={{ marginTop: 0 }}>크립토</h2>
          <p style={{ color: "var(--fg-3)", lineHeight: 1.5 }}>
            Upbit KRW 마켓 읽기 전용 화면입니다. 주문·감시·동기화 작업은 실행하지 않습니다.
          </p>
          {data?.pendingOrders && (
            <p style={{ color: "var(--fg-3)", fontSize: 13 }}>
              미체결 {data.pendingOrders.items.length}건 · 보유 {data.holdings?.heldCount ?? 0}종목
            </p>
          )}
        </aside>
      }
      center={
        <main data-testid="crypto-dashboard" style={{ padding: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 16 }}>
            <div>
              <h1 style={{ margin: 0 }}>크립토 대시보드</h1>
              <p style={{ marginTop: 6, color: "var(--fg-3)" }}>KRW 마켓 시세, 호가 스프레드, 보유/미체결 상태</p>
            </div>
            <Link to="/screener" className="screener-chip">스크리너로 이동</Link>
          </div>
          {err && <div style={{ color: "var(--danger)", marginTop: 16 }}>오류: {err}</div>}
          {!data && !err && <div style={{ padding: 16, color: "var(--fg-3)" }}>불러오는 중...</div>}
          {data && (
            <>
              {data.meta.warnings.length > 0 && (
                <ul className="screener-warnings" aria-label="crypto warnings">
                  {data.meta.warnings.map((warning) => <li key={warning}>{warning}</li>)}
                </ul>
              )}
              <RiskSummary cards={data.cards} sources={data.meta.sources} />
              <CandidateInsights candidates={data.insights.candidates ?? []} sources={data.meta.sources} />
              <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 12, marginTop: 16 }}>
                {data.cards.map((card) => <CryptoCard key={card.symbol} card={card} sources={data.meta.sources} />)}
              </section>
              {reference ? <ReferencePanel reference={reference} /> : <ReferenceFallback />}
              <section style={{ marginTop: 18, padding: 16, border: "1px solid var(--border)", borderRadius: 14 }}>
                <h2 style={{ marginTop: 0 }}>기능 상태</h2>
                <p style={{ color: "var(--fg-3)" }}>체결/주문 실행: {data.capabilities.execution.state} · 실시간 스트리밍: {data.capabilities.liveStreaming.state}</p>
                <p style={{ color: "var(--fg-3)" }}>최근 체결/프로젝트 정보는 참고용 또는 외부 데이터 공백으로 표시됩니다.</p>
              </section>
            </>
          )}
        </main>
      }
      right={<RightRemotePanel />}
    />
  );
}

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightRemotePanel } from "../../desktop/RightRemotePanel";
import { fetchCryptoDashboard } from "../../api/investCrypto";
import type { CryptoDashboardResponse, CryptoMarketCard } from "../../types/investCrypto";
import "../../desktop/screener/screener.css";

function formatKrw(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "-";
  return `${Math.round(value).toLocaleString("ko-KR")}원`;
}

function formatPct(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "-";
  return `${(value * 100).toFixed(2)}%`;
}

function cardMetric(card: CryptoMarketCard): string {
  if (card.accTradePrice24h === null) return "거래대금 -";
  if (card.accTradePrice24h >= 1_0000_0000) {
    return `거래대금 ${(card.accTradePrice24h / 1_0000_0000).toFixed(1)}억`;
  }
  return `거래대금 ${Math.round(card.accTradePrice24h).toLocaleString("ko-KR")}`;
}

function CryptoCard({ card }: { card: CryptoMarketCard }) {
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

function cryptoErrorMessage(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error ?? "");
  if (/Failed to fetch|NetworkError|Load failed|crypto\/dashboard \d{3}/i.test(text)) {
    return "크립토 대시보드 데이터를 일시적으로 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }
  return text || "크립토 대시보드 데이터를 일시적으로 불러오지 못했습니다.";
}

export function DesktopCryptoPage() {
  const [data, setData] = useState<CryptoDashboardResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();

  useEffect(() => {
    let cancel = false;
    setErr(undefined);
    fetchCryptoDashboard({ limit: 20 })
      .then((response) => {
        if (cancel) return;
        setData(response);
      })
      .catch((error) => !cancel && setErr(cryptoErrorMessage(error)));
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
              <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 12, marginTop: 16 }}>
                {data.cards.map((card) => <CryptoCard key={card.symbol} card={card} />)}
              </section>
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

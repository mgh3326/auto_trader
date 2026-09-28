import type {
  TraderBroker,
  TraderMarket,
  TraderOpenOrderRow,
  TraderOpenOrderSource,
  TraderOpenOrdersResponse,
} from "./types";
import { fmtDateTime, fmtNum } from "./format";

const BROKER_ORDER: TraderBroker[] = ["kis", "toss", "upbit"];
const BROKER_LABEL: Record<TraderBroker, string> = {
  kis: "KIS",
  toss: "Toss",
  upbit: "Upbit",
};
const MARKET_LABEL: Record<TraderMarket, string> = {
  kr: "KR",
  us: "US",
  crypto: "Crypto",
};

function sourceStatusLabel(source: TraderOpenOrderSource): string {
  if (source.status === "ok") return "정상";
  if (source.status === "degraded") return "일부 실패";
  return "조회 불가";
}

function BrokerSection({
  broker,
  sources,
  items,
  notes,
}: {
  broker: TraderBroker;
  sources: TraderOpenOrderSource[];
  items: TraderOpenOrderRow[];
  notes: string[];
}) {
  const mine = sources.filter((s) => s.broker === broker);
  const rows = items.filter((r) => r.broker === broker);
  const allUnavailable = mine.length > 0 && mine.every((s) => s.status === "unavailable");
  const lastOk = mine
    .map((s) => s.last_ok_at)
    .filter((v): v is string => Boolean(v))
    .sort()
    .at(-1);

  return (
    <section className="trader-broker" data-testid={`broker-${broker}`}>
      <header className="trader-broker-head">
        <h3>{BROKER_LABEL[broker]}</h3>
        {mine.map((s) => (
          <span
            key={`${s.broker}-${s.market}`}
            className={`trader-chip trader-chip-${s.status}`}
            data-testid={`source-state-${s.broker}-${s.market}`}
          >
            {MARKET_LABEL[s.market]} · {sourceStatusLabel(s)} · {s.count}건
          </span>
        ))}
      </header>

      {allUnavailable ? (
        <p className="trader-unavailable" data-testid={`unavailable-${broker}`}>
          {BROKER_LABEL[broker]} 미체결 조회 불가 — 마지막 성공 조회:{" "}
          {lastOk ? fmtDateTime(lastOk) : "기록 없음"}
          {mine.some((s) => s.message) ? (
            <span className="trader-dim"> ({mine.map((s) => s.message).filter(Boolean).join("; ")})</span>
          ) : null}
        </p>
      ) : rows.length === 0 ? (
        <p className="trader-dim">미체결 주문 없음</p>
      ) : (
        <table className="trader-table">
          <thead>
            <tr>
              <th>시장</th>
              <th>종목</th>
              <th>구분</th>
              <th>수량</th>
              <th>미체결</th>
              <th>가격</th>
              <th>주문시각</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.broker}-${row.order_no}`}>
                <td>{MARKET_LABEL[row.market]}</td>
                <td>
                  {row.symbol_name ? `${row.symbol_name} ` : ""}
                  <span className="trader-dim">{row.symbol}</span>
                </td>
                <td>{row.side === "buy" ? "매수" : row.side === "sell" ? "매도" : "-"}</td>
                <td className="num">{fmtNum(row.quantity)}</td>
                <td className="num">{fmtNum(row.remaining_qty)}</td>
                <td className="num">
                  {fmtNum(row.price)}
                  {row.currency ? ` ${row.currency}` : ""}
                </td>
                <td>{fmtDateTime(row.ordered_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {broker === "toss"
        ? notes.map((note) => (
            <p key={note} className="trader-note" data-testid="toss-visibility-note">
              {note}
            </p>
          ))
        : null}
    </section>
  );
}

export function OpenOrdersPanel({
  data,
  loading,
  error,
  onRefresh,
}: {
  data: TraderOpenOrdersResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  return (
    <div className="trader-panel" data-testid="panel-open-orders">
      <header className="trader-panel-head">
        <h2>미체결 주문</h2>
        <button type="button" onClick={onRefresh} disabled={loading}>
          {loading ? "조회 중…" : "새로고침"}
        </button>
      </header>
      {data ? (
        <p className="trader-dim">
          기준 {fmtDateTime(data.as_of)} · 캐시 TTL {data.cache.ttl_seconds}s
          {data.cache.hit ? " · 캐시됨" : " · 방금 조회"}
        </p>
      ) : null}
      {error ? <p className="trader-error">조회 실패: {error}</p> : null}
      {data
        ? BROKER_ORDER.map((broker) => (
            <BrokerSection
              key={broker}
              broker={broker}
              sources={data.sources}
              items={data.items}
              notes={data.notes}
            />
          ))
        : null}
      {!data && !error && !loading ? <p className="trader-dim">불러오는 중…</p> : null}
    </div>
  );
}

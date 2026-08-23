// /invest/buy-plan — 매수 계획 (트리거 보드), §144차.
//
// One body for both shells, same pattern as WatchesPageBody. Read-only: this
// page has no control that places, approves, or cancels anything.
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { fetchBuyPlan } from "../../api/buyPlan";
import type { BuyPlanMarketFilter, BuyPlanResponse } from "../../types/buyPlan";
import { PageSafetyNote } from "../PageSafetyNote";
import { FundingBlock } from "./FundingBlock";
import {
  ActiveWatchBlock,
  ApprovalNotice,
  AveragingBlock,
  DiscoveryGateBlock,
  SupportNetBlock,
} from "./TriggerBlocks";
import { dateTime } from "./format";

const MARKET_OPTIONS: { key: BuyPlanMarketFilter; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

const VALID_MARKETS = MARKET_OPTIONS.map((o) => o.key);

function paramOrDefault<T extends string>(
  raw: string | null,
  valid: readonly T[],
  fallback: T,
): T {
  return raw && (valid as readonly string[]).includes(raw) ? (raw as T) : fallback;
}

function FilterRow({
  value,
  onChange,
}: Readonly<{
  value: BuyPlanMarketFilter;
  onChange: (key: BuyPlanMarketFilter) => void;
}>) {
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {MARKET_OPTIONS.map((option) => {
        const active = value === option.key;
        return (
          <button
            key={option.key}
            type="button"
            onClick={() => onChange(option.key)}
            style={{
              border: "none",
              borderRadius: 999,
              padding: "6px 12px",
              fontSize: 12,
              fontWeight: 700,
              cursor: "pointer",
              fontFamily: "inherit",
              background: active ? "var(--fg)" : "var(--surface-2)",
              color: active ? "var(--bg)" : "var(--fg-2)",
            }}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function BuyPlanPageBody() {
  const [searchParams] = useSearchParams();
  const [market, setMarket] = useState<BuyPlanMarketFilter>(() =>
    paramOrDefault(searchParams.get("market"), VALID_MARKETS, "all"),
  );
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; data: BuyPlanResponse }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchBuyPlan(market)
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            message: err instanceof Error ? err.message : String(err),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  return (
    <div style={{ display: "grid", gap: 18 }}>
      <div style={{ display: "grid", gap: 10 }}>
        <h1 style={{ margin: 0, fontSize: 20 }}>매수 계획</h1>
        <FilterRow value={market} onChange={setMarket} />
      </div>

      <PageSafetyNote
        routeId="buy-plan"
        heading="이 화면은 판정이 아니라 자금 준비용 근사입니다"
        tag="read-only"
        items={[
          "여기 숫자는 정책 산식의 표시용 근사이며, 매수 판정의 정본은 회차입니다.",
          "이 화면은 주문·제안·워치를 만들거나 승인하지 않습니다.",
          "이미 걸린 지정가는 브로커가 현금을 이미 묶고 있어 '입금 필요액'에서 제외했습니다.",
          "현금 대조는 계좌별입니다 — 다른 브로커의 같은 통화 잔고는 합산하지 않습니다.",
          "레인 표시는 cap 기준 분류이며 승인 확정이 아닙니다.",
        ]}
      />

      {state.status === "loading" && (
        <p style={{ fontSize: 13, color: "var(--fg-2)" }}>불러오는 중…</p>
      )}

      {state.status === "error" && (
        <p style={{ fontSize: 13, color: "var(--loss)" }}>
          불러오지 못했습니다 — {state.message}
        </p>
      )}

      {state.status === "ready" && (
        <>
          {state.data.warnings.length > 0 && (
            <ul
              style={{
                margin: 0,
                paddingLeft: 18,
                fontSize: 12,
                color: "var(--warn)",
                display: "grid",
                gap: 4,
              }}
            >
              {state.data.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}

          <FundingBlock funding={state.data.funding} />
          <ApprovalNotice approval={state.data.approval_context} />
          <AveragingBlock
            rows={state.data.averaging_triggers}
            approval={state.data.approval_context}
          />
          <SupportNetBlock tier={state.data.support_net} />
          <ActiveWatchBlock
            rows={state.data.active_buy_watches}
            approval={state.data.approval_context}
          />
          <DiscoveryGateBlock rows={state.data.discovery_gates} />

          <details>
            <summary
              style={{ cursor: "pointer", fontSize: 12, color: "var(--fg-2)" }}
            >
              값 출처 ({state.data.value_sources.length})
            </summary>
            <div style={{ display: "grid", gap: 6, marginTop: 8, fontSize: 11 }}>
              {state.data.value_sources.map((source) => (
                <div key={source.field}>
                  <code style={{ color: "var(--fg)" }}>{source.field}</code>
                  <span style={{ color: "var(--fg-2)" }}> ← {source.source}</span>
                  {source.note && (
                    <div style={{ color: "var(--fg-2)" }}>{source.note}</div>
                  )}
                </div>
              ))}
            </div>
          </details>

          <footer style={{ fontSize: 11, color: "var(--fg-2)" }}>
            정책 {state.data.policy.version} (
            <code>{state.data.policy.content_hash.slice(0, 12)}</code>) · 계산{" "}
            {dateTime(state.data.as_of)} · 캐시 {state.data.cache_ttl_seconds}초
          </footer>
        </>
      )}
    </div>
  );
}

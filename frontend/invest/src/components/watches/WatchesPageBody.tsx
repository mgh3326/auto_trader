// Shared fetch + filter + grouped-ladder body for the /invest/watches page
// (INVEST-WATCH-UI §57차 item ①). One body renders inside both
// MobileWatchesPage (MobileShell) and DesktopWatchesPage (DesktopShell) —
// the layout difference between viewports is entirely in the shell, not this
// content, since a single-column card stack already reads fine at desktop
// width (unlike WatchAlertsPanel's wide table, which needs horizontal room).
import { useEffect, useMemo, useState } from "react";
import { fetchWatches } from "../../api/watches";
import { Pill } from "../../ds";
import type { WatchesResponse, WatchMarket, WatchStatus } from "../../types/watches";
import { PageSafetyNote } from "../PageSafetyNote";
import { WatchGroupCard } from "./WatchGroupCard";
import { groupWatchesBySymbol } from "./watchGrouping";

const MARKET_OPTIONS: { key: WatchMarket; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

const STATUS_OPTIONS: { key: WatchStatus; label: string }[] = [
  { key: "active", label: "감시중" },
  { key: "all", label: "전체 상태" },
  { key: "triggered", label: "감시발화" },
  { key: "expired", label: "만료됨" },
  { key: "canceled", label: "취소됨" },
];

function FilterRow<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { key: T; label: string }[];
  value: T;
  onChange: (key: T) => void;
}) {
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {options.map((option) => {
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

export function WatchesPageBody() {
  // Default to "active" — this is a live browsing surface for what's being
  // watched right now, not a history log (that's what status=all is for).
  const [market, setMarket] = useState<WatchMarket>("all");
  const [status, setStatus] = useState<WatchStatus>("active");
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; data: WatchesResponse }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchWatches(market, status)
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [market, status]);

  const groups = useMemo(
    () => (state.status === "ready" ? groupWatchesBySymbol(state.data.items) : []),
    [state],
  );
  const dataState = state.status === "ready" ? state.data.data_state : null;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "grid", gap: 6 }}>
        <h1 style={{ margin: 0, fontSize: 22, letterSpacing: "-0.03em" }}>감시 목록</h1>
        <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.6 }}>
          종목별로 묶은 AI 감시 트리거입니다. 하나의 종목에 여러 단계 조건(래더)이 있으면 한 카드에 함께 표시됩니다.
        </p>
      </div>

      <PageSafetyNote
        routeId="watches"
        heading="읽기 전용"
        tag="Phase 1"
        items={["주문·승인·watch 등록/수정 mutation을 호출하지 않습니다.", "감시 조건 변경은 이 화면에서 할 수 없습니다."]}
      />

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div style={{ display: "grid", gap: 8 }}>
          <FilterRow options={MARKET_OPTIONS} value={market} onChange={setMarket} />
          <FilterRow options={STATUS_OPTIONS} value={status} onChange={setStatus} />
        </div>
        {dataState && (
          <Pill tone={dataState === "ok" ? "accent" : dataState === "degraded" ? "warn" : "loss"} size="sm">
            {dataState === "ok" ? "실시간" : dataState === "degraded" ? "시세 지연" : "확인 불가"}
          </Pill>
        )}
      </div>

      {state.status === "ready" && state.data.warnings.length > 0 && (
        <div
          role="alert"
          style={{
            padding: "8px 10px",
            borderRadius: 10,
            background: "var(--warn-soft)",
            color: "var(--warn)",
            fontSize: 12,
          }}
        >
          {state.data.warnings.join(" · ")}
        </div>
      )}

      {state.status === "loading" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>감시 목록을 불러오는 중…</div>
      )}

      {state.status === "error" && (
        <div role="alert" style={{ padding: 16, color: "var(--danger)", fontSize: 13 }}>
          감시 목록을 불러오지 못했습니다. {state.message}
        </div>
      )}

      {state.status === "ready" && groups.length === 0 && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
          {state.data.empty_reason ?? "표시할 감시 항목이 없습니다."}
        </div>
      )}

      {groups.length > 0 && (
        <div data-testid="watch-group-list" style={{ display: "grid", gap: 12 }}>
          {groups.map((group) => (
            <WatchGroupCard key={`${group.market}:${group.symbol}`} group={group} />
          ))}
        </div>
      )}
    </div>
  );
}

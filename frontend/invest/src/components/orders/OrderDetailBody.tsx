// Shared status-dispatch body for the standalone order detail page
// (INVEST-WATCH-UI §57차 item ②) — used by both the mobile and desktop shell
// wrappers.
import { PageSafetyNote } from "../PageSafetyNote";
import { OrderDetailCard } from "./OrderDetailCard";
import { useOrderDetail } from "./useOrderDetail";

export function OrderDetailBody() {
  const state = useOrderDetail();

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "grid", gap: 6 }}>
        <h1 style={{ margin: 0, fontSize: 22, letterSpacing: "-0.03em" }}>주문 상세</h1>
        <p style={{ margin: 0, color: "var(--fg-2)", fontSize: 13, lineHeight: 1.6 }}>
          체결 원장과 사유(thesis)를 결합한 읽기 전용 상세 화면입니다.
        </p>
      </div>

      <PageSafetyNote
        routeId="order-detail"
        heading="읽기 전용"
        tag="Phase 1"
        items={["주문 정정·취소·재실행 mutation을 호출하지 않습니다."]}
      />

      {state.status === "loading" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>불러오는 중…</div>
      )}
      {state.status === "not_found" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
          해당 주문을 찾을 수 없습니다.
        </div>
      )}
      {state.status === "error" && (
        <div role="alert" style={{ padding: 16, color: "var(--danger)", fontSize: 13 }}>
          주문 상세를 불러오지 못했습니다. {state.message}
        </div>
      )}
      {state.status === "ready" && <OrderDetailCard order={state.order} />}
    </div>
  );
}

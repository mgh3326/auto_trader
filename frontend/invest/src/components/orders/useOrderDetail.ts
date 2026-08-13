// Shared fetch hook for the /invest/orders/:broker/:ledgerId detail route
// (INVEST-WATCH-UI §57차 item ②).
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchOrderDetail, OrderDetailNotFoundError } from "../../api/orderDetail";
import type { LinkedOrder } from "../../types/investmentReports";

export type OrderDetailState =
  | { status: "loading" }
  | { status: "ready"; order: LinkedOrder }
  | { status: "not_found" }
  | { status: "error"; message: string };

export function useOrderDetail(): OrderDetailState {
  const { broker, ledgerId } = useParams<{ broker: string; ledgerId: string }>();
  const [state, setState] = useState<OrderDetailState>({ status: "loading" });

  useEffect(() => {
    const numericLedgerId = Number(ledgerId);
    if (!broker || !Number.isFinite(numericLedgerId)) {
      setState({ status: "not_found" });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    fetchOrderDetail(broker, numericLedgerId)
      .then((order) => {
        if (!cancelled) setState({ status: "ready", order });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof OrderDetailNotFoundError) {
          setState({ status: "not_found" });
        } else {
          setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [broker, ledgerId]);

  return state;
}

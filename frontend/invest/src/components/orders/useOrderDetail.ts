// Shared fetch hook for the /invest/orders/:broker/:market/:ledgerId detail
// route (INVEST-WATCH-UI §57차 item ②).
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import {
  fetchOrderDetail,
  OrderDetailNotFoundError,
  OrderDetailUnknownLedgerError,
} from "../../api/orderDetail";
import type { LinkedOrder } from "../../types/investmentReports";

export type OrderDetailState =
  | { status: "loading" }
  | { status: "ready"; order: LinkedOrder }
  | { status: "not_found" }
  | { status: "error"; message: string };

export function useOrderDetail(): OrderDetailState {
  // `market` is required alongside `broker` — verify-r1 BLOCKER-1: broker
  // alone (e.g. "kis") is ambiguous between the KR domestic ledger and the
  // US live ledger, which have independent id sequences.
  const { broker, market, ledgerId } = useParams<{
    broker: string;
    market: string;
    ledgerId: string;
  }>();
  const [state, setState] = useState<OrderDetailState>({ status: "loading" });

  useEffect(() => {
    const numericLedgerId = Number(ledgerId);
    if (!broker || !market || !Number.isFinite(numericLedgerId)) {
      setState({ status: "not_found" });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    fetchOrderDetail(broker, market, numericLedgerId)
      .then((order) => {
        if (!cancelled) setState({ status: "ready", order });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof OrderDetailNotFoundError || err instanceof OrderDetailUnknownLedgerError) {
          setState({ status: "not_found" });
        } else {
          setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [broker, market, ledgerId]);

  return state;
}

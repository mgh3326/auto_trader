export type CurrentOrdersMarket = "all" | "kr" | "us" | "crypto";
export type CurrentOrderRowMarket = "kr" | "us" | "crypto";
export type CurrentOrderBroker = "kis" | "toss" | "upbit";
export type CurrentOrderSide = "buy" | "sell" | "unknown";
export type CurrentOrdersDataState = "ok" | "degraded" | "unavailable";

export interface CurrentOrderRow {
  broker: CurrentOrderBroker;
  market: CurrentOrderRowMarket;
  symbol: string;
  symbol_name: string | null;
  side: CurrentOrderSide;
  order_type: string | null;
  time_in_force: string | null;
  price: string | null;
  quantity: string | null;
  remaining_qty: string | null;
  filled_qty: string | null;
  status: string;
  raw_status: string | null;
  ordered_at: string | null;
  order_no: string;
  exchange: string | null;
  currency: string | null;
}

export interface CurrentOrderSourceState {
  broker: CurrentOrderBroker;
  market: CurrentOrderRowMarket;
  status: CurrentOrdersDataState;
  fetched_at: string | null;
  count: number;
  message: string | null;
}

export interface CurrentOrdersResponse {
  market: CurrentOrdersMarket;
  count: number;
  data_state: CurrentOrdersDataState;
  as_of: string;
  items: CurrentOrderRow[];
  sources: CurrentOrderSourceState[];
  warnings: string[];
  empty_reason: string | null;
}

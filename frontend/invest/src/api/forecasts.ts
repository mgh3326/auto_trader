import type {
  CalibrationResponse,
  ForecastGroupBy,
  ForecastListResponse,
} from "../types/forecasts";

const BASE = "/trading/api/invest/forecasts";

export interface CalibrationQuery {
  groupBy?: ForecastGroupBy;
  createdBy?: string;
  symbol?: string;
  instrumentType?: string;
  days?: number;
}

export async function fetchForecastCalibration(
  q: CalibrationQuery = {},
): Promise<CalibrationResponse> {
  const params = new URLSearchParams();
  if (q.groupBy) params.set("group_by", q.groupBy);
  if (q.createdBy) params.set("created_by", q.createdBy);
  if (q.symbol) params.set("symbol", q.symbol);
  if (q.instrumentType) params.set("instrument_type", q.instrumentType);
  if (q.days != null) params.set("days", String(q.days));
  const qs = params.toString();
  const res = await fetch(`${BASE}/calibration${qs ? `?${qs}` : ""}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`forecast calibration ${res.status}`);
  return res.json();
}

export interface ForecastListQuery {
  symbol?: string;
  createdBy?: string;
  instrumentType?: string;
  limit?: number;
}

async function fetchForecastList(
  kind: "open" | "closed",
  q: ForecastListQuery = {},
): Promise<ForecastListResponse> {
  const params = new URLSearchParams();
  if (q.symbol) params.set("symbol", q.symbol);
  if (q.createdBy) params.set("created_by", q.createdBy);
  if (q.instrumentType) params.set("instrument_type", q.instrumentType);
  if (q.limit != null) params.set("limit", String(q.limit));
  const qs = params.toString();
  const res = await fetch(`${BASE}/${kind}${qs ? `?${qs}` : ""}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`forecast ${kind} ${res.status}`);
  return res.json();
}

export function fetchOpenForecasts(
  q: ForecastListQuery = {},
): Promise<ForecastListResponse> {
  return fetchForecastList("open", q);
}

export function fetchClosedForecasts(
  q: ForecastListQuery = {},
): Promise<ForecastListResponse> {
  return fetchForecastList("closed", q);
}

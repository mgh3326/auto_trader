import { useCallback, useEffect, useState } from "react";

import {
  fetchTraderFillsToday,
  fetchTraderOpenOrders,
  fetchTraderWatches,
} from "./api";
import { ActiveWatchesPanel } from "./ActiveWatchesPanel";
import { FillsTodayPanel } from "./FillsTodayPanel";
import { OpenOrdersPanel } from "./OpenOrdersPanel";
import type {
  TraderFillsResponse,
  TraderOpenOrdersResponse,
  TraderWatchesResponse,
} from "./types";
import "./trader.css";

type PanelState<T> = {
  data: T | null;
  loading: boolean;
  error: string | null;
};

function idle<T>(): PanelState<T> {
  return { data: null, loading: true, error: null };
}

function toError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function TraderPage() {
  const [openOrders, setOpenOrders] = useState<PanelState<TraderOpenOrdersResponse>>(idle);
  const [fills, setFills] = useState<PanelState<TraderFillsResponse>>(idle);
  const [watches, setWatches] = useState<PanelState<TraderWatchesResponse>>(idle);

  const loadOpenOrders = useCallback(async (refresh = false) => {
    setOpenOrders((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await fetchTraderOpenOrders(refresh);
      setOpenOrders({ data, loading: false, error: null });
    } catch (err) {
      setOpenOrders((s) => ({ ...s, loading: false, error: toError(err) }));
    }
  }, []);

  const loadFills = useCallback(async () => {
    setFills((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await fetchTraderFillsToday();
      setFills({ data, loading: false, error: null });
    } catch (err) {
      setFills((s) => ({ ...s, loading: false, error: toError(err) }));
    }
  }, []);

  const loadWatches = useCallback(async () => {
    setWatches((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await fetchTraderWatches();
      setWatches({ data, loading: false, error: null });
    } catch (err) {
      setWatches((s) => ({ ...s, loading: false, error: toError(err) }));
    }
  }, []);

  useEffect(() => {
    void loadOpenOrders();
    void loadFills();
    void loadWatches();
  }, [loadOpenOrders, loadFills, loadWatches]);

  return (
    <main className="trader-page">
      <header className="trader-page-head">
        <h1>운영자 현황</h1>
        <p className="trader-dim">읽기 전용 · trader.robinco.dev</p>
      </header>
      <OpenOrdersPanel
        data={openOrders.data}
        loading={openOrders.loading}
        error={openOrders.error}
        onRefresh={() => void loadOpenOrders(true)}
      />
      <FillsTodayPanel
        data={fills.data}
        loading={fills.loading}
        error={fills.error}
        onRefresh={() => void loadFills()}
      />
      <ActiveWatchesPanel
        data={watches.data}
        loading={watches.loading}
        error={watches.error}
        onRefresh={() => void loadWatches()}
      />
    </main>
  );
}

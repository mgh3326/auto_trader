import { useCallback, useEffect, useState } from "react";
import type { AccountPanelResponse } from "../types/invest";
import { fetchAccountPanel } from "../api/accountPanel";

export function useAccountPanel() {
  const [data, setData] = useState<AccountPanelResponse | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancel = false;
    setError(undefined);
    setLoading(true);
    fetchAccountPanel()
      .then((r) => { if (!cancel) { setData(r); setLoading(false); } })
      .catch((e) => { if (!cancel) { setError(String(e?.message ?? e)); setLoading(false); } });
    return () => { cancel = true; };
  }, [tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

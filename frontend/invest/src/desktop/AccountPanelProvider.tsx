import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { AccountPanelResponse } from "../types/invest";
import { fetchAccountPanel } from "../api/accountPanel";

export interface AccountPanelContextValue {
  data: AccountPanelResponse | undefined;
  error: string | undefined;
  loading: boolean;
  refreshing: boolean;
  lastLoadedAt: number | undefined;
  reload: () => void;
}

const AccountPanelContext = createContext<AccountPanelContextValue | null>(null);

export function AccountPanelProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [data, setData] = useState<AccountPanelResponse | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastLoadedAt, setLastLoadedAt] = useState<number | undefined>();
  const [tick, setTick] = useState(0);
  const hasFetched = useRef(false);

  useEffect(() => {
    let cancel = false;
    setError(undefined);
    if (hasFetched.current) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    fetchAccountPanel()
      .then((r) => {
        if (cancel) return;
        setData(r);
        setLoading(false);
        setRefreshing(false);
        setLastLoadedAt(Date.now());
        hasFetched.current = true;
      })
      .catch((e: unknown) => {
        if (cancel) return;
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        setLoading(false);
        setRefreshing(false);
        hasFetched.current = true;
      });
    return () => {
      cancel = true;
    };
  }, [tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const value = useMemo(
    () => ({ data, error, loading, refreshing, lastLoadedAt, reload }),
    [data, error, loading, refreshing, lastLoadedAt, reload],
  );

  return (
    <AccountPanelContext.Provider value={value}>
      {children}
    </AccountPanelContext.Provider>
  );
}

export function useAccountPanelContext(): AccountPanelContextValue {
  const ctx = useContext(AccountPanelContext);
  if (!ctx) throw new Error("useAccountPanelContext must be used within AccountPanelProvider");
  return ctx;
}

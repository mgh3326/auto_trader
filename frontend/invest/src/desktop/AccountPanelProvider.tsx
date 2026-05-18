import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { AccountPanelResponse } from "../types/invest";
import { fetchAccountPanel, type FetchAccountPanelOptions } from "../api/accountPanel";

export interface AccountPanelLoadOptions {
  includePaper?: boolean;
  paperSources?: readonly string[];
}

export interface AccountPanelContextValue {
  data: AccountPanelResponse | undefined;
  error: string | undefined;
  loading: boolean;
  refreshing: boolean;
  lastLoadedAt: number | undefined;
  /** Currently-loaded paper sources (empty unless includePaper was passed). */
  loadedPaperSources: readonly string[];
  /** Lazy fetch entry-point. Safe to call multiple times. */
  load: (options?: AccountPanelLoadOptions) => void;
  /** Re-fetch with the last successful params. No-op if never loaded. */
  reload: () => void;
}

const AccountPanelContext = createContext<AccountPanelContextValue | null>(null);

export function AccountPanelProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [data, setData] = useState<AccountPanelResponse | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [lastLoadedAt, setLastLoadedAt] = useState<number | undefined>();
  const [loadedPaperSources, setLoadedPaperSources] = useState<readonly string[]>([]);

  const lastOptionsRef = useRef<AccountPanelLoadOptions | null>(null);
  const inflightRef = useRef<AbortController | null>(null);
  const hasLoadedRef = useRef(false);

  const doFetch = useCallback((opts: AccountPanelLoadOptions) => {
    inflightRef.current?.abort();
    const controller = new AbortController();
    inflightRef.current = controller;

    setError(undefined);
    if (hasLoadedRef.current) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    const apiOpts: FetchAccountPanelOptions = {
      signal: controller.signal,
      includePaper: opts.includePaper,
      paperSources: opts.paperSources,
    };

    fetchAccountPanel(apiOpts)
      .then((r) => {
        if (controller.signal.aborted) return;
        setData(r);
        setLoading(false);
        setRefreshing(false);
        setLastLoadedAt(Date.now());
        setLoadedPaperSources(opts.paperSources ? [...opts.paperSources] : []);
        hasLoadedRef.current = true;
        lastOptionsRef.current = opts;
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        setLoading(false);
        setRefreshing(false);
        hasLoadedRef.current = true;
        lastOptionsRef.current = opts;
      });
  }, []);

  const load = useCallback(
    (options: AccountPanelLoadOptions = {}) => {
      doFetch(options);
    },
    [doFetch],
  );

  const reload = useCallback(() => {
    // Lazy mode: do not auto-fetch unless we have previously loaded.
    if (!hasLoadedRef.current || lastOptionsRef.current === null) return;
    doFetch(lastOptionsRef.current);
  }, [doFetch]);

  const value = useMemo(
    () => ({
      data,
      error,
      loading,
      refreshing,
      lastLoadedAt,
      loadedPaperSources,
      load,
      reload,
    }),
    [data, error, loading, refreshing, lastLoadedAt, loadedPaperSources, load, reload],
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

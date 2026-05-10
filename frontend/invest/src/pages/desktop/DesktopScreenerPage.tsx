import { useEffect, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightRemotePanel } from "../../desktop/RightRemotePanel";
import { ScreenerPresetSidebar } from "../../desktop/screener/ScreenerPresetSidebar";
import { ScreenerFilterBar } from "../../desktop/screener/ScreenerFilterBar";
import { ScreenerResultsTable } from "../../desktop/screener/ScreenerResultsTable";
import { ScreenerFilterModal } from "../../desktop/screener/ScreenerFilterModal";
import { ScreenerFreshnessLine } from "../../desktop/screener/ScreenerFreshnessLine";
import { fetchScreenerPresets, fetchScreenerResults } from "../../api/screener";
import type {
  ScreenerMarket,
  ScreenerPresetsResponse,
  ScreenerResultsResponse,
} from "../../types/screener";
import "../../desktop/screener/screener.css";

export function DesktopScreenerPage() {
  const [presets, setPresets] = useState<ScreenerPresetsResponse | undefined>();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedMarket, setSelectedMarket] = useState<Extract<ScreenerMarket, "kr" | "us">>("kr");
  const [results, setResults] = useState<ScreenerResultsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [modalOpen, setModalOpen] = useState(false);

  useEffect(() => {
    let cancel = false;
    fetchScreenerPresets()
      .then((r) => {
        if (cancel) return;
        setErr(undefined);
        setPresets(r);
        setSelectedId(r.selectedPresetId ?? r.presets[0]?.id ?? null);
      })
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    let cancel = false;
    setResults(undefined);
    setErr(undefined);
    fetchScreenerResults(selectedId, selectedMarket)
      .then((r) => {
        if (cancel) return;
        setErr(undefined);
        setResults(r);
      })
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, [selectedId, selectedMarket]);

  return (
    <DesktopShell
      left={
        <ScreenerPresetSidebar
          presets={presets?.presets ?? []}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
      }
      center={
        <div data-testid="screener-center">
          <div className="screener-market-toggle" role="group" aria-label="시장 선택">
            <button
              type="button"
              className={selectedMarket === "kr" ? "is-active" : ""}
              aria-pressed={selectedMarket === "kr"}
              onClick={() => setSelectedMarket("kr")}
            >
              국내
            </button>
            <button
              type="button"
              className={selectedMarket === "us" ? "is-active" : ""}
              aria-pressed={selectedMarket === "us"}
              onClick={() => setSelectedMarket("us")}
            >
              미국
            </button>
          </div>
          {err && <div style={{ color: "var(--danger)", marginBottom: 12 }}>오류: {err}</div>}
          {results ? (
            <>
              <ScreenerFilterBar
                title={results.title}
                description={results.description}
                chips={results.filterChips}
                resultCount={results.results.length}
                onOpenFilterModal={() => setModalOpen(true)}
              />
              <ScreenerFreshnessLine freshness={results.freshness} />
              {results.warnings.length > 0 && (
                <ul className="screener-warnings" aria-label="warnings">
                  {results.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              )}
              <ScreenerResultsTable rows={results.results} metricLabel={results.metricLabel} />
            </>
          ) : (
            !err && <div style={{ padding: 16, color: "var(--fg-3)" }}>불러오는 중...</div>
          )}
          <ScreenerFilterModal
            open={modalOpen}
            onClose={() => setModalOpen(false)}
            appliedChipCount={results?.filterChips.length ?? 0}
          />
        </div>
      }
      right={<RightRemotePanel />}
    />
  );
}

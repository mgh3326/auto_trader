import { useEffect, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
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

function screenerErrorMessage(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error ?? "");
  if (
    /Failed to fetch|NetworkError|Load failed|screener\/results \d{3}|screener\/presets \d{3}/i.test(text)
  ) {
    return "스크리너 데이터를 일시적으로 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }
  return text || "스크리너 데이터를 일시적으로 불러오지 못했습니다.";
}

export function DesktopScreenerPage() {
  const [presets, setPresets] = useState<ScreenerPresetsResponse | undefined>();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedMarket, setSelectedMarket] = useState<ScreenerMarket>("kr");
  const [results, setResults] = useState<ScreenerResultsResponse | undefined>();
  const [err, setErr] = useState<string | undefined>();
  const [modalOpen, setModalOpen] = useState(false);

  useEffect(() => {
    let cancel = false;
    setErr(undefined);
    setPresets(undefined);
    setSelectedId(null);
    setResults(undefined);
    fetchScreenerPresets(selectedMarket)
      .then((r) => {
        if (cancel) return;
        setErr(undefined);
        setPresets(r);
        setSelectedId(r.selectedPresetId ?? r.presets[0]?.id ?? null);
      })
      .catch((e) => !cancel && setErr(screenerErrorMessage(e)));
    return () => { cancel = true; };
  }, [selectedMarket]);

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
      .catch((e) => !cancel && setErr(screenerErrorMessage(e)));
    return () => { cancel = true; };
  }, [selectedId, selectedMarket]);

  const handleMarketChange = (market: ScreenerMarket) => {
    if (market === selectedMarket) return;
    setPresets(undefined);
    setSelectedId(null);
    setResults(undefined);
    setSelectedMarket(market);
  };

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
              onClick={() => handleMarketChange("kr")}
            >
              국내
            </button>
            <button
              type="button"
              className={selectedMarket === "us" ? "is-active" : ""}
              aria-pressed={selectedMarket === "us"}
              onClick={() => handleMarketChange("us")}
            >
              미국
            </button>
            <button
              type="button"
              className={selectedMarket === "crypto" ? "is-active" : ""}
              aria-pressed={selectedMarket === "crypto"}
              onClick={() => handleMarketChange("crypto")}
            >
              가상자산
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
              <ScreenerResultsTable
                rows={results.results}
                metricLabel={results.metricLabel}
                freshness={results.freshness}
              />
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
    />
  );
}

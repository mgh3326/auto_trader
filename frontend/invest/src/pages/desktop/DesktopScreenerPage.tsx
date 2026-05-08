import { useEffect, useState } from "react";
import { DesktopShell } from "../../desktop/DesktopShell";
import { RightAccountPanel } from "../../desktop/RightAccountPanel";
import { useAccountPanel } from "../../desktop/useAccountPanel";
import { ScreenerPresetSidebar } from "../../desktop/screener/ScreenerPresetSidebar";
import { ScreenerFilterBar } from "../../desktop/screener/ScreenerFilterBar";
import { ScreenerResultsTable } from "../../desktop/screener/ScreenerResultsTable";
import { ScreenerFilterModal } from "../../desktop/screener/ScreenerFilterModal";
import { fetchScreenerPresets, fetchScreenerResults } from "../../api/screener";
import type {
  ScreenerPresetsResponse,
  ScreenerResultsResponse,
} from "../../types/screener";
import "../../desktop/screener/screener.css";

export function DesktopScreenerPage() {
  const panel = useAccountPanel();
  const [presets, setPresets] = useState<ScreenerPresetsResponse | undefined>();
  const [selectedId, setSelectedId] = useState<string | null>(null);
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
    fetchScreenerResults(selectedId)
      .then((r) => {
        if (cancel) return;
        setErr(undefined);
        setResults(r);
      })
      .catch((e) => !cancel && setErr(String(e?.message ?? e)));
    return () => { cancel = true; };
  }, [selectedId]);

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
          {err && <div style={{ color: "#f59e9e", marginBottom: 12 }}>오류: {err}</div>}
          {results ? (
            <>
              <ScreenerFilterBar
                title={results.title}
                description={results.description}
                chips={results.filterChips}
                resultCount={results.results.length}
                onOpenFilterModal={() => setModalOpen(true)}
              />
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
            !err && <div style={{ padding: 16, color: "#9ba0ab" }}>불러오는 중...</div>
          )}
          <ScreenerFilterModal
            open={modalOpen}
            onClose={() => setModalOpen(false)}
            appliedChipCount={results?.filterChips.length ?? 0}
          />
        </div>
      }
      right={<RightAccountPanel data={panel.data} loading={panel.loading} error={panel.error} />}
    />
  );
}

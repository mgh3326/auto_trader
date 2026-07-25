// ROB-592 — per-symbol watch card for the stock detail page.
// Reuses the ROB-591 watch read endpoint (filtered by symbol) and the shared
// presentation helpers so a symbol's active/triggered watches render with the
// same condition + proximity semantics as the /invest/my 감시 tab.

import { Card, Pill } from "../../ds";
import {
  PROXIMITY_BAND_LABELS,
  PROXIMITY_BAND_TONES,
  WATCH_STATUS_LABELS,
  WATCH_STATUS_TONES,
  formatWatchCondition,
  formatWatchDateTime,
  formatWatchMoney,
} from "../../components/my/watchPresentation";
import type { WatchAlertRow } from "../../types/watches";

export function WatchCard({ watches }: { watches: WatchAlertRow[] | undefined }) {
  return (
    <Card data-testid="stock-detail-watch">
      <h2 style={{ margin: "0 0 4px", fontSize: 16 }}>감시</h2>
      <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--fg-3)" }}>
        이 종목의 AI 감시 트리거 (조건 · 근접도 · 발화)
      </p>
      {!watches ? (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>불러오는 중입니다…</p>
      ) : null}
      {watches && watches.length === 0 ? (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>등록된 감시가 없습니다.</p>
      ) : null}
      {watches && watches.length > 0 ? (
        <div style={{ display: "grid", gap: 8 }}>
          {watches.map((row) => (
            <div
              key={row.alert_uuid}
              style={{
                border: "1px solid var(--divider)",
                borderRadius: 12,
                padding: "10px 12px",
                display: "grid",
                gap: 6,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                  <Pill tone={WATCH_STATUS_TONES[row.status] ?? "paper"} size="sm">
                    {WATCH_STATUS_LABELS[row.status] ?? row.status}
                  </Pill>
                  {row.near_expiry ? (
                    <Pill tone="warn" size="sm">임박</Pill>
                  ) : null}
                  {row.status === "active" && row.proximity_band ? (
                    <Pill tone={PROXIMITY_BAND_TONES[row.proximity_band] ?? "paper"} size="sm">
                      {PROXIMITY_BAND_LABELS[row.proximity_band] ?? row.proximity_band}
                    </Pill>
                  ) : null}
                </div>
                <div style={{ fontSize: 13, fontWeight: 700, fontFeatureSettings: '"tnum"' }}>
                  {row.current_price ? formatWatchMoney(row.current_price, row.market) : null}
                </div>
              </div>
              <div style={{ fontSize: 13 }}>{formatWatchCondition(row)}</div>
              <div style={{ fontSize: 11, color: "var(--fg-3)" }}>
                만료: {formatWatchDateTime(row.valid_until)} · {row.intent} · {row.action_mode}
              </div>
              {row.rationale ? (
                <div style={{ fontSize: 12, color: "var(--fg-2)" }}>{row.rationale}</div>
              ) : null}
              {row.last_event ? (
                <div style={{ fontSize: 11, color: "var(--fg-3)" }}>
                  발화: {formatWatchDateTime(row.last_event.created_at)} ({row.last_event.outcome})
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}

// One symbol's watch-alert ladder — the per-card unit of the /invest/watches
// browsing page (INVEST-WATCH-UI §57차 item ①). Mobile-first: a single-column
// card stack rather than the wide table WatchAlertsPanel uses, so it reads
// cleanly at phone width without horizontal scroll.

import { Link } from "react-router-dom";
import { Card, Pill } from "../../ds";
import { stockDetailPath } from "../../stockDetailPath";
import type { WatchAlertRow } from "../../types/watches";
import {
  PROXIMITY_BAND_LABELS,
  PROXIMITY_BAND_TONES,
  WATCH_MARKET_LABEL,
  WATCH_STATUS_LABELS,
  WATCH_STATUS_TONES,
  formatWatchCondition,
  formatWatchDateTime,
  formatWatchMoney,
} from "../my/watchPresentation";
import type { WatchGroup } from "./watchGrouping";
import { formatDistancePct, hasMaxAction } from "./watchGrouping";

function LadderRung({ row }: { row: WatchAlertRow }) {
  const distance = formatDistancePct(row);
  return (
    <div
      data-testid="watch-ladder-rung"
      style={{
        display: "grid",
        gap: 4,
        padding: "10px 12px",
        borderRadius: 10,
        background: "var(--surface-2)",
      }}
    >
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        <Pill tone={WATCH_STATUS_TONES[row.status] ?? "paper"} size="sm">
          {WATCH_STATUS_LABELS[row.status] ?? row.status}
        </Pill>
        {row.status === "active" && row.proximity_band && (
          <Pill tone={PROXIMITY_BAND_TONES[row.proximity_band] ?? "paper"} size="sm">
            {PROXIMITY_BAND_LABELS[row.proximity_band] ?? row.proximity_band}
          </Pill>
        )}
        {row.near_expiry && <Pill tone="warn" size="sm">임박</Pill>}
        <Pill tone="paper" size="sm">{row.intent}</Pill>
        {hasMaxAction(row) && <Pill tone="accent" size="sm">실행플랜</Pill>}
      </div>
      <div style={{ fontSize: 13, fontWeight: 600 }}>{formatWatchCondition(row)}</div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 11, color: "var(--fg-3)" }}>
        {distance != null && <span>목표까지 {distance}</span>}
        <span>만료: {formatWatchDateTime(row.valid_until)}</span>
        {row.last_event && <span>발화: {formatWatchDateTime(row.last_event.created_at)} ({row.last_event.outcome})</span>}
      </div>
      {row.rationale && (
        <div style={{ fontSize: 12, color: "var(--fg-2)", lineHeight: 1.5 }}>{row.rationale}</div>
      )}
    </div>
  );
}

export function WatchGroupCard({ group }: { group: WatchGroup }) {
  const href = stockDetailPath(group.market, group.symbol);
  const dispName = group.symbolName && group.symbolName !== group.symbol ? group.symbolName : group.symbol;

  const header = (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8 }}>
      <div>
        <div style={{ fontSize: 15, fontWeight: 800 }}>{dispName}</div>
        <div style={{ fontSize: 11, color: "var(--fg-3)" }}>
          {group.symbol} · {WATCH_MARKET_LABEL[group.market]}
          {group.items.length > 1 ? ` · ${group.items.length}단계 래더` : ""}
        </div>
      </div>
      {group.currentPrice && (
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 10, color: "var(--fg-3)" }}>현재가</div>
          <div style={{ fontSize: 14, fontWeight: 700, fontFeatureSettings: '"tnum"' }}>
            {formatWatchMoney(group.currentPrice, group.market)}
          </div>
        </div>
      )}
    </div>
  );

  return (
    <Card data-testid="watch-group-card">
      <div style={{ display: "grid", gap: 10 }}>
        {href ? (
          <Link to={href} style={{ color: "inherit", textDecoration: "none" }}>
            {header}
          </Link>
        ) : (
          header
        )}
        <div style={{ display: "grid", gap: 6 }}>
          {group.items.map((row) => (
            <LadderRung key={row.alert_uuid} row={row} />
          ))}
        </div>
      </div>
    </Card>
  );
}

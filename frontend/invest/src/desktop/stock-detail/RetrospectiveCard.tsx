// ROB-662 — per-symbol retrospective card for the stock detail page.
// Reuses the /invest/retrospectives read endpoint (filtered by symbol) so a
// symbol's postmortems (trigger/root cause/lesson) and incomplete next actions
// render alongside the ROB-592 watch card.

import { Card, Pill } from "../../ds";
import type { RetrospectiveRow } from "../../types/retrospectives";

function pnlText(row: RetrospectiveRow): string {
  if (row.realized_pnl == null) return "—";
  const sign = row.realized_pnl > 0 ? "+" : "";
  return `${sign}${row.realized_pnl.toLocaleString("ko-KR")} ${row.realized_pnl_currency ?? ""}`.trim();
}

// ROB-885 — explicit active allowlist. Only open|in_progress render; missing,
// unknown, and terminal (done/obsolete/expired) statuses are inactive.
const ACTIVE_ACTION_STATUSES = new Set(["open", "in_progress"]);

function activeActions(row: RetrospectiveRow): string[] {
  return (row.next_actions ?? [])
    .filter((a) => ACTIVE_ACTION_STATUSES.has(String((a as { status?: string }).status ?? "")))
    .map((a) => String((a as { action?: unknown }).action ?? ""))
    .filter(Boolean);
}

export function RetrospectiveCard({ retrospectives }: { retrospectives: RetrospectiveRow[] | undefined }) {
  return (
    <Card data-testid="stock-detail-retrospective">
      <h2 style={{ margin: "0 0 4px", fontSize: 16 }}>회고</h2>
      <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--fg-3)" }}>
        이 종목의 매매 회고 (트리거 · 원인 · 교훈 · 미완료 액션)
      </p>
      {!retrospectives ? (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>불러오는 중입니다…</p>
      ) : null}
      {retrospectives && retrospectives.length === 0 ? (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>등록된 회고가 없습니다.</p>
      ) : null}
      {retrospectives && retrospectives.length > 0 ? (
        <div style={{ display: "grid", gap: 8 }}>
          {retrospectives.map((row) => {
            const actions = activeActions(row);
            return (
              <div key={row.id} style={{ border: "1px solid var(--divider)", borderRadius: 12, padding: "10px 12px", display: "grid", gap: 6 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                    {row.trigger_type ? <Pill tone="paper" size="sm">{row.trigger_type}</Pill> : null}
                    {row.root_cause_class ? <Pill tone="paper" size="sm">{row.root_cause_class}</Pill> : null}
                  </div>
                  <div style={{ fontSize: 13, fontWeight: 700, fontFeatureSettings: '"tnum"' }}>{pnlText(row)}</div>
                </div>
                {row.lesson || row.result_summary ? (
                  <div style={{ fontSize: 13, color: "var(--fg-2)" }}>{row.lesson ?? row.result_summary}</div>
                ) : null}
                {actions.length > 0 ? (
                  <div style={{ display: "grid", gap: 4 }}>
                    {actions.map((a, idx) => (
                      <div key={idx} style={{ fontSize: 12, display: "flex", gap: 6, alignItems: "center" }}>
                        <Pill tone="accent" size="sm">액션</Pill>
                        <span>{a}</span>
                      </div>
                    ))}
                  </div>
                ) : null}
                {row.created_at ? (
                  <div style={{ fontSize: 11, color: "var(--fg-3)" }}>{row.created_at.slice(0, 10)}</div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </Card>
  );
}

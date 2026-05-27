// ROB-335 — intraday ActionPacket surface: four headers (held / new / risk /
// data-gap) + sub-verdict chips. Pure view-layer over the bundle.actionPacket
// projection (parallel to ROB-322 ReviewSectionsView).
import { Pill, type PillTone } from "../../ds/atoms";
import type {
  ActionPacket,
  ActionPacketEntry,
  ActionVerdict,
  DataGapEntry,
  NoActionSummary,
} from "../../types/investmentReports";

const VERDICT_LABELS: Record<ActionVerdict, string> = {
  buy_review: "신규매수 검토",
  limit_wait: "지정가 대기",
  no_new_buy_candidates: "신규 후보 없음",
  sell_review: "매도 검토",
  trim_review: "축소 검토",
  add_review: "추가매수 검토",
  keep: "유지",
  no_add: "추가매수 금지",
  watch_only: "관망",
  rejected: "제외",
  data_gap: "데이터 부족",
};

const VERDICT_TONES: Record<ActionVerdict, PillTone> = {
  buy_review: "gain",
  add_review: "gain",
  limit_wait: "warn",
  no_new_buy_candidates: "paper",
  sell_review: "loss",
  trim_review: "loss",
  keep: "paper",
  no_add: "paper",
  watch_only: "accent",
  rejected: "warn",
  data_gap: "warn",
};

function EntryRow({ entry }: { entry: ActionPacketEntry }) {
  return (
    <div style={{ display: "grid", gap: 4, padding: "6px 0" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Pill tone={VERDICT_TONES[entry.verdict]} size="sm">
          {VERDICT_LABELS[entry.verdict]}
        </Pill>
        {entry.symbol ? (
          <strong style={{ fontSize: 14 }}>{entry.symbol}</strong>
        ) : null}
      </div>
      <p style={{ margin: 0, fontSize: 13 }}>{entry.rationale}</p>
    </div>
  );
}

function PacketSection({
  title,
  entries,
  emptyReason,
}: {
  title: string;
  entries: ActionPacketEntry[];
  emptyReason?: string | null;
}) {
  return (
    <section style={{ display: "grid", gap: 6 }}>
      <h2 style={{ margin: 0, fontSize: 18 }}>
        {title} ({entries.length})
      </h2>
      {entries.length > 0 ? (
        entries.map((entry, idx) => (
          <EntryRow key={entry.itemUuid ?? `${entry.verdict}-${idx}`} entry={entry} />
        ))
      ) : (
        <p style={{ margin: 0, fontSize: 13 }}>{emptyReason ?? "해당 없음"}</p>
      )}
    </section>
  );
}

function DataGapSection({
  gaps,
  noActionReason,
}: {
  gaps: DataGapEntry[];
  noActionReason?: NoActionSummary | null;
}) {
  return (
    <section style={{ display: "grid", gap: 6 }}>
      <h2 style={{ margin: 0, fontSize: 18 }}>데이터 부족 ({gaps.length})</h2>
      {noActionReason?.reasonKo ? (
        <p style={{ margin: 0, fontSize: 13 }}>{noActionReason.reasonKo}</p>
      ) : null}
      {gaps.length > 0 ? (
        gaps.map((gap, idx) => (
          <div key={`${gap.source}-${idx}`} style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Pill tone="warn" size="sm">{gap.source}</Pill>
            <span style={{ fontSize: 13 }}>
              {[gap.status, gap.reason].filter(Boolean).join(" · ")}
            </span>
          </div>
        ))
      ) : (
        <p style={{ margin: 0, fontSize: 13 }}>해당 없음</p>
      )}
    </section>
  );
}

export function ActionPacketView({ packet }: { packet: ActionPacket }) {
  return (
    <section data-testid="action-packet" style={{ display: "grid", gap: 16 }}>
      <PacketSection title="오늘의 보유 액션" entries={packet.heldActions} />
      <PacketSection
        title="신규 후보"
        entries={packet.newBuyCandidates}
        emptyReason={packet.noNewBuyReason}
      />
      <PacketSection title="리스크" entries={packet.riskReviews} />
      <DataGapSection
        gaps={packet.dataGapsForNextCycle}
        noActionReason={packet.noActionReason}
      />
    </section>
  );
}

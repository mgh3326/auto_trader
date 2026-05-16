import { Pill } from "../../ds";

type Tone = "paper" | "accent" | "gain" | "loss" | "warn";

const STATUS_LABELS: Record<string, string> = {
  published: "발행됨",
  ok: "정상",
  approved: "승인됨",
  filled: "체결됨",
  rejected: "거절됨",
  failed: "실패",
  blocked: "차단됨",
  awaiting_approval: "승인 대기",
  degraded: "부분 확인",
  stale: "오래됨",
  unavailable: "확인 필요",
  not_submitted: "미제출",
};

function toneForStatus(status: string): Tone {
  if (["published", "ok", "approved", "filled"].includes(status)) return "gain";
  if (["rejected", "failed", "blocked"].includes(status)) return "loss";
  if (["awaiting_approval", "degraded", "stale", "unavailable", "not_submitted"].includes(status)) return "warn";
  return "paper";
}

export function statusText(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function StatusBadge({ label, status }: { label?: string; status: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      {label && <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{label}</span>}
      <Pill tone={toneForStatus(status)} size="sm">{statusText(status)}</Pill>
    </span>
  );
}

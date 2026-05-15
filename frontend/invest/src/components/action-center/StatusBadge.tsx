import { Pill } from "../../ds";

type Tone = "paper" | "accent" | "gain" | "loss" | "warn";

function toneForStatus(status: string): Tone {
  if (["published", "ok", "approved", "filled"].includes(status)) return "gain";
  if (["rejected", "failed", "blocked"].includes(status)) return "loss";
  if (["awaiting_approval", "degraded", "stale", "unavailable", "not_submitted"].includes(status)) return "warn";
  return "paper";
}

export function StatusBadge({ label, status }: { label?: string; status: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      {label && <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{label}</span>}
      <Pill tone={toneForStatus(status)} size="sm">{status}</Pill>
    </span>
  );
}

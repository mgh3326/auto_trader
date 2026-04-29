import { useState } from "react";
import type { FormEvent } from "react";
import type {
  StrategyEventCreateRequest,
  Uuid,
} from "../api/types";
import styles from "./OperatorEventForm.module.css";

interface OperatorEventFormProps {
  sessionUuid: Uuid;
  onSubmit: (
    body: StrategyEventCreateRequest,
  ) => Promise<{ ok: boolean; status?: number; detail?: string }>;
}

function clamp(value: number, lo: number, hi: number): number {
  if (Number.isNaN(value)) return lo;
  return Math.max(lo, Math.min(hi, value));
}

function splitSymbols(raw: string): string[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export default function OperatorEventForm({
  sessionUuid,
  onSubmit,
}: OperatorEventFormProps) {
  const [sourceText, setSourceText] = useState("");
  const [symbolsRaw, setSymbolsRaw] = useState("");
  const [severity, setSeverity] = useState("2");
  const [confidence, setConfidence] = useState("50");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const trimmed = sourceText.trim();
    if (!trimmed) {
      setError("Source text is required.");
      return;
    }

    const body: StrategyEventCreateRequest = {
      source: "user",
      event_type: "operator_market_event",
      source_text: trimmed,
      session_uuid: sessionUuid,
      severity: clamp(Number(severity), 1, 5),
      confidence: clamp(Number(confidence), 0, 100),
    };

    const symbols = splitSymbols(symbolsRaw);
    if (symbols.length > 0) body.affected_symbols = symbols;

    setSubmitting(true);
    const res = await onSubmit(body);
    setSubmitting(false);
    if (!res.ok) {
      setError(res.detail ?? "Could not submit strategy event.");
      return;
    }
    setSourceText("");
    setSymbolsRaw("");
    setSeverity("2");
    setConfidence("50");
  }

  return (
    <form
      className={styles.form}
      onSubmit={handleSubmit}
      aria-label="Add operator market event"
      noValidate
    >
      <label className={styles.fullWidth}>
        Source text
        <textarea
          value={sourceText}
          onChange={(e) => setSourceText(e.target.value)}
          placeholder="e.g. OpenAI earnings missed expectations"
        />
      </label>

      <label className={styles.fullWidth}>
        Affected symbols (comma-separated, optional)
        <input
          value={symbolsRaw}
          onChange={(e) => setSymbolsRaw(e.target.value)}
          placeholder="e.g. MSFT, NVDA"
        />
      </label>

      <label>
        Severity (1–5)
        <input
          type="number"
          min={1}
          max={5}
          step={1}
          value={severity}
          onChange={(e) => setSeverity(e.target.value)}
        />
      </label>

      <label>
        Confidence (0–100)
        <input
          type="number"
          min={0}
          max={100}
          step={1}
          value={confidence}
          onChange={(e) => setConfidence(e.target.value)}
        />
      </label>

      {error ? (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      ) : null}

      <button type="submit" disabled={submitting}>
        {submitting ? "Submitting..." : "Add event"}
      </button>
    </form>
  );
}

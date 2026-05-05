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
      setError("소스 텍스트는 필수입니다.");
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
      setError(res.detail ?? "전략 이벤트를 제출할 수 없습니다.");
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
      aria-label="운영자 시장 이벤트 추가"
      noValidate
    >
      <label className={styles.fullWidth}>
        소스 텍스트
        <textarea
          value={sourceText}
          onChange={(e) => setSourceText(e.target.value)}
          placeholder="예: OpenAI 실적이 기대치 하회"
        />
      </label>

      <label className={styles.fullWidth}>
        영향 종목 (쉼표로 구분, 선택)
        <input
          value={symbolsRaw}
          onChange={(e) => setSymbolsRaw(e.target.value)}
          placeholder="예: MSFT, NVDA"
        />
      </label>

      <label>
        심각도 (1–5)
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
        신뢰도 (0–100)
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
        {submitting ? "제출 중…" : "이벤트 추가"}
      </button>
    </form>
  );
}

import { useState } from "react";
import type { FormEvent } from "react";
import type {
  CounterfactualDetail,
  OutcomeCreateRequest,
  OutcomeHorizon,
  TrackKind,
} from "../api/types";
import { COMMON, OUTCOME_HORIZON_LABEL, TRACK_KIND_LABEL } from "../i18n";
import styles from "./OutcomeMarkForm.module.css";

const TRACKS: TrackKind[] = [
  "accepted_live",
  "accepted_paper",
  "rejected_counterfactual",
  "analyst_alternative",
  "user_alternative",
];
const HORIZONS: OutcomeHorizon[] = ["1h", "4h", "1d", "3d", "7d", "final"];

interface OutcomeMarkFormProps {
  counterfactuals: CounterfactualDetail[];
  onSubmit: (
    body: OutcomeCreateRequest,
  ) => Promise<{ ok: boolean; detail?: string }>;
}

export default function OutcomeMarkForm({
  counterfactuals,
  onSubmit,
}: OutcomeMarkFormProps) {
  const [trackKind, setTrackKind] = useState<TrackKind>("accepted_live");
  const [horizon, setHorizon] = useState<OutcomeHorizon>("1h");
  const [price, setPrice] = useState("");
  const [pnlPct, setPnlPct] = useState("");
  const [pnlAmount, setPnlAmount] = useState("");
  const [counterfactualId, setCounterfactualId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (!price || !Number.isFinite(Number(price)) || Number(price) < 0) {
      setError("마크 시점 가격은 0 이상의 숫자여야 합니다");
      return;
    }
    if (trackKind === "accepted_live" && counterfactualId) {
      setError("accepted_live 트랙은 대조군을 선택할 수 없습니다");
      return;
    }
    if (trackKind !== "accepted_live" && !counterfactualId) {
      setError("이 트랙에서는 대조군이 필요합니다");
      return;
    }

    const body: OutcomeCreateRequest = {
      track_kind: trackKind,
      horizon,
      price_at_mark: price,
      marked_at: new Date().toISOString(),
    };
    if (counterfactualId) body.counterfactual_id = Number(counterfactualId);
    if (pnlPct) body.pnl_pct = pnlPct;
    if (pnlAmount) body.pnl_amount = pnlAmount;

    setSubmitting(true);
    const res = await onSubmit(body);
    setSubmitting(false);
    if (!res.ok) {
      setError(res.detail ?? "결과 마크를 기록할 수 없습니다.");
      return;
    }
    setPrice("");
    setPnlPct("");
    setPnlAmount("");
  }

  return (
    <form
      className={styles.form}
      onSubmit={handleSubmit}
      aria-label="결과 마크 기록"
    >
      <label>
        트랙
        <select
          value={trackKind}
          onChange={(e) => {
            const v = e.target.value as TrackKind;
            setTrackKind(v);
            if (v === "accepted_live") setCounterfactualId("");
          }}
        >
          {TRACKS.map((t) => (
            <option key={t} value={t}>
              {TRACK_KIND_LABEL[t]}
            </option>
          ))}
        </select>
      </label>

      <label>
        기간
        <select
          value={horizon}
          onChange={(e) => setHorizon(e.target.value as OutcomeHorizon)}
        >
          {HORIZONS.map((h) => (
            <option key={h} value={h}>
              {OUTCOME_HORIZON_LABEL[h]}
            </option>
          ))}
        </select>
      </label>

      {trackKind !== "accepted_live" ? (
        <label>
          대조군
          <select
            value={counterfactualId}
            onChange={(e) => setCounterfactualId(e.target.value)}
          >
            <option value="">— 선택 —</option>
            {counterfactuals
              .filter((c) => c.track_kind === trackKind)
              .map((c) => (
                <option key={c.id} value={c.id}>
                  #{c.id} · 기준가 {c.baseline_price}
                </option>
              ))}
          </select>
        </label>
      ) : null}

      <label>
        마크 시점 가격
        <input
          value={price}
          onChange={(e) => setPrice(e.target.value)}
          placeholder="예: 118000000"
        />
      </label>

      <label>
        손익(%)
        <input
          value={pnlPct}
          onChange={(e) => setPnlPct(e.target.value)}
          placeholder="선택"
        />
      </label>

      <label>
        손익 금액
        <input
          value={pnlAmount}
          onChange={(e) => setPnlAmount(e.target.value)}
          placeholder="선택"
        />
      </label>

      {error ? (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      ) : null}

      <button type="submit" disabled={submitting}>
        {submitting ? COMMON.saving : "마크 기록"}
      </button>
    </form>
  );
}

import { useState } from "react";
import type { FormEvent } from "react";
import type {
  CounterfactualDetail,
  OutcomeCreateRequest,
  OutcomeHorizon,
  TrackKind,
} from "../api/types";
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
      setError("price_at_mark must be a non-negative number");
      return;
    }
    if (trackKind === "accepted_live" && counterfactualId) {
      setError("accepted_live must not have a counterfactual selected");
      return;
    }
    if (trackKind !== "accepted_live" && !counterfactualId) {
      setError("counterfactual is required for this track");
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
      setError(res.detail ?? "Could not record outcome mark.");
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
      aria-label="Record outcome mark"
    >
      <label>
        Track
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
              {t}
            </option>
          ))}
        </select>
      </label>

      <label>
        Horizon
        <select
          value={horizon}
          onChange={(e) => setHorizon(e.target.value as OutcomeHorizon)}
        >
          {HORIZONS.map((h) => (
            <option key={h} value={h}>
              {h}
            </option>
          ))}
        </select>
      </label>

      {trackKind !== "accepted_live" ? (
        <label>
          Counterfactual
          <select
            value={counterfactualId}
            onChange={(e) => setCounterfactualId(e.target.value)}
          >
            <option value="">— select —</option>
            {counterfactuals
              .filter((c) => c.track_kind === trackKind)
              .map((c) => (
                <option key={c.id} value={c.id}>
                  #{c.id} · baseline {c.baseline_price}
                </option>
              ))}
          </select>
        </label>
      ) : null}

      <label>
        Price at mark
        <input
          value={price}
          onChange={(e) => setPrice(e.target.value)}
          placeholder="e.g. 118000000"
        />
      </label>

      <label>
        PnL %
        <input
          value={pnlPct}
          onChange={(e) => setPnlPct(e.target.value)}
          placeholder="optional"
        />
      </label>

      <label>
        PnL amount
        <input
          value={pnlAmount}
          onChange={(e) => setPnlAmount(e.target.value)}
          placeholder="optional"
        />
      </label>

      {error ? (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      ) : null}

      <button type="submit" disabled={submitting}>
        {submitting ? "Saving..." : "Record mark"}
      </button>
    </form>
  );
}

import { useState } from "react";
import type {
  ProposalDetail,
  ProposalRespondRequest,
  RespondAction,
} from "../api/types";
import styles from "./ProposalAdjustmentEditor.module.css";

type AdjustResponse = Extract<RespondAction, "modify" | "partial_accept">;
type NumericField = Exclude<keyof ProposalRespondRequest, "response" | "user_note">;

interface FieldSpec {
  label: string;
  userKey: NumericField;
  originalKey: keyof Pick<
    ProposalDetail,
    | "original_quantity"
    | "original_quantity_pct"
    | "original_amount"
    | "original_price"
    | "original_trigger_price"
    | "original_threshold_pct"
  >;
  percent?: boolean;
  nonNegative?: boolean;
}

const specs: FieldSpec[] = [
  { label: "Quantity", userKey: "user_quantity", originalKey: "original_quantity" },
  {
    label: "Quantity percent",
    userKey: "user_quantity_pct",
    originalKey: "original_quantity_pct",
    percent: true,
  },
  {
    label: "Amount",
    userKey: "user_amount",
    originalKey: "original_amount",
    nonNegative: true,
  },
  {
    label: "Price",
    userKey: "user_price",
    originalKey: "original_price",
    nonNegative: true,
  },
  {
    label: "Trigger price",
    userKey: "user_trigger_price",
    originalKey: "original_trigger_price",
    nonNegative: true,
  },
  {
    label: "Threshold percent",
    userKey: "user_threshold_pct",
    originalKey: "original_threshold_pct",
    percent: true,
  },
];

interface ProposalAdjustmentEditorProps {
  proposal: ProposalDetail;
  response: AdjustResponse;
  onCancel: () => void;
  onSubmit: (
    body: ProposalRespondRequest,
  ) => Promise<{ ok: boolean; detail?: string }>;
}

const decimalPattern = /^-?\d+(\.\d+)?$/;

export default function ProposalAdjustmentEditor({
  proposal,
  response,
  onCancel,
  onSubmit,
}: ProposalAdjustmentEditorProps) {
  const [values, setValues] = useState<Record<NumericField, string>>({
    user_quantity: "",
    user_quantity_pct: "",
    user_amount: "",
    user_price: "",
    user_trigger_price: "",
    user_threshold_pct: "",
  });
  const [userNote, setUserNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const visibleSpecs = specs.filter((spec) => proposal[spec.originalKey] !== null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);

    const body: ProposalRespondRequest = { response };
    let hasNumeric = false;
    for (const spec of visibleSpecs) {
      const value = values[spec.userKey].trim();
      if (!value) continue;
      if (!decimalPattern.test(value)) {
        setError(`${spec.label} must be a decimal string.`);
        return;
      }
      const parsed = Number(value);
      if (spec.nonNegative && parsed < 0) {
        setError(`${spec.label} must be greater than or equal to 0.`);
        return;
      }
      if (spec.percent && (parsed < 0 || parsed > 100)) {
        setError(`${spec.label} must be between 0 and 100.`);
        return;
      }
      body[spec.userKey] = value;
      hasNumeric = true;
    }

    if (!hasNumeric) {
      setError("Enter at least one adjusted numeric value.");
      return;
    }
    if (userNote.trim()) body.user_note = userNote.trim();

    setIsSubmitting(true);
    const result = await onSubmit(body);
    setIsSubmitting(false);
    if (!result.ok) setError(result.detail ?? "Something went wrong. Try again.");
  }

  return (
    <form className={styles.editor} onSubmit={handleSubmit}>
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.grid}>
        {visibleSpecs.map((spec) => (
          <label className={styles.field} key={spec.userKey}>
            <span>{spec.label}</span>
            <input
              inputMode="decimal"
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  [spec.userKey]: event.target.value,
                }))
              }
              pattern="[0-9.\-]*"
              placeholder={proposal[spec.originalKey] ?? undefined}
              type="text"
              value={values[spec.userKey]}
            />
          </label>
        ))}
      </div>
      <label className={styles.field}>
        <span>Note</span>
        <textarea
          maxLength={4000}
          onChange={(event) => setUserNote(event.target.value)}
          rows={3}
          value={userNote}
        />
      </label>
      <div className={styles.actions}>
        <button className="btn btn-primary" disabled={isSubmitting} type="submit">
          Save {response === "partial_accept" ? "partial accept" : "modify"}
        </button>
        <button className="btn btn-ghost" disabled={isSubmitting} onClick={onCancel} type="button">
          Cancel
        </button>
      </div>
    </form>
  );
}

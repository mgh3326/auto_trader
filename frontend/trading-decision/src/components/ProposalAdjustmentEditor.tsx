import { useState } from "react";
import type {
  ProposalDetail,
  ProposalRespondRequest,
  RespondAction,
} from "../api/types";
import { COMMON } from "../i18n";
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
  { label: "수량", userKey: "user_quantity", originalKey: "original_quantity" },
  {
    label: "수량 비율(%)",
    userKey: "user_quantity_pct",
    originalKey: "original_quantity_pct",
    percent: true,
  },
  {
    label: "금액",
    userKey: "user_amount",
    originalKey: "original_amount",
    nonNegative: true,
  },
  {
    label: "가격",
    userKey: "user_price",
    originalKey: "original_price",
    nonNegative: true,
  },
  {
    label: "트리거 가격",
    userKey: "user_trigger_price",
    originalKey: "original_trigger_price",
    nonNegative: true,
  },
  {
    label: "임계 비율(%)",
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
        setError(`${spec.label}은(는) 소수 문자열이어야 합니다.`);
        return;
      }
      const parsed = Number(value);
      if (spec.nonNegative && parsed < 0) {
        setError(`${spec.label}은(는) 0 이상이어야 합니다.`);
        return;
      }
      if (spec.percent && (parsed < 0 || parsed > 100)) {
        setError(`${spec.label}은(는) 0 이상 100 이하이어야 합니다.`);
        return;
      }
      body[spec.userKey] = value;
      hasNumeric = true;
    }

    if (!hasNumeric) {
      setError("조정된 숫자 값을 하나 이상 입력해 주세요.");
      return;
    }
    if (userNote.trim()) body.user_note = userNote.trim();

    setIsSubmitting(true);
    const result = await onSubmit(body);
    setIsSubmitting(false);
    if (!result.ok) setError(result.detail ?? COMMON.somethingWentWrong);
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
        <span>메모</span>
        <textarea
          maxLength={4000}
          onChange={(event) => setUserNote(event.target.value)}
          rows={3}
          value={userNote}
        />
      </label>
      <div className={styles.actions}>
        <button className="btn btn-primary" disabled={isSubmitting} type="submit">
          {response === "partial_accept" ? "부분 수락 저장" : "수정 저장"}
        </button>
        <button className="btn btn-ghost" disabled={isSubmitting} onClick={onCancel} type="button">
          취소
        </button>
      </div>
    </form>
  );
}

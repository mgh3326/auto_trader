import type { RespondAction, UserResponseValue } from "../api/types";
import { RESPONSE_BUTTON_LABEL } from "../i18n";

interface ProposalResponseControlsProps {
  currentResponse: UserResponseValue;
  isSubmitting: boolean;
  onSimpleResponse: (response: "accept" | "reject" | "defer") => void;
  onOpenAdjust: (response: "modify" | "partial_accept") => void;
}

const buttons: Array<{ value: RespondAction; kind: "simple" | "adjust" }> = [
  { value: "accept", kind: "simple" },
  { value: "partial_accept", kind: "adjust" },
  { value: "modify", kind: "adjust" },
  { value: "defer", kind: "simple" },
  { value: "reject", kind: "simple" },
];

export default function ProposalResponseControls({
  currentResponse,
  isSubmitting,
  onSimpleResponse,
  onOpenAdjust,
}: ProposalResponseControlsProps) {
  return (
    <div className="response-controls" aria-label="제안 응답 컨트롤">
      {buttons.map((button) => (
        <button
          aria-pressed={currentResponse === button.value}
          className={currentResponse === button.value ? "btn btn-primary" : "btn"}
          disabled={isSubmitting}
          key={button.value}
          onClick={() => {
            if (button.value === "modify" || button.value === "partial_accept") {
              onOpenAdjust(button.value);
            } else {
              onSimpleResponse(button.value as "accept" | "reject" | "defer");
            }
          }}
          type="button"
        >
          {RESPONSE_BUTTON_LABEL[button.value]}
        </button>
      ))}
    </div>
  );
}

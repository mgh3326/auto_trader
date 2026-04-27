import type { RespondAction, UserResponseValue } from "../api/types";

interface ProposalResponseControlsProps {
  currentResponse: UserResponseValue;
  isSubmitting: boolean;
  onSimpleResponse: (response: "accept" | "reject" | "defer") => void;
  onOpenAdjust: (response: "modify" | "partial_accept") => void;
}

const buttons: Array<{
  label: string;
  value: RespondAction;
  kind: "simple" | "adjust";
}> = [
  { label: "Accept", value: "accept", kind: "simple" },
  { label: "Partial accept", value: "partial_accept", kind: "adjust" },
  { label: "Modify", value: "modify", kind: "adjust" },
  { label: "Defer", value: "defer", kind: "simple" },
  { label: "Reject", value: "reject", kind: "simple" },
];

export default function ProposalResponseControls({
  currentResponse,
  isSubmitting,
  onSimpleResponse,
  onOpenAdjust,
}: ProposalResponseControlsProps) {
  return (
    <div className="response-controls" aria-label="Proposal response controls">
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
              onSimpleResponse(button.value);
            }
          }}
          type="button"
        >
          {button.label}
        </button>
      ))}
    </div>
  );
}

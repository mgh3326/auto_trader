import { COMMON } from "../i18n";

interface ErrorViewProps {
  message: string;
  onRetry?: () => void;
}

export default function ErrorView({ message, onRetry }: ErrorViewProps) {
  return (
    <div className="surface-message" role="alert">
      <p>{message}</p>
      {onRetry ? <button onClick={onRetry}>{COMMON.retry}</button> : null}
    </div>
  );
}

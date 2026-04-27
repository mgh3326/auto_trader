interface SummaryPair {
  label: string;
  original: string | null;
  user: string | null;
}

interface OriginalVsAdjustedSummaryProps {
  pairs: SummaryPair[];
}

export default function OriginalVsAdjustedSummary({
  pairs,
}: OriginalVsAdjustedSummaryProps) {
  return (
    <dl>
      {pairs.map((pair) => (
        <div key={pair.label}>
          <dt>{pair.label}</dt>
          <dd>
            <span>{pair.original ?? "—"}</span>
            {" → "}
            <strong>{pair.user ?? "(unchanged)"}</strong>
          </dd>
        </div>
      ))}
    </dl>
  );
}

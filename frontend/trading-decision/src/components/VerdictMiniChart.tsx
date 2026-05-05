import type { SymbolTimelineEntry } from "../api/types";

interface Props {
  entries: SymbolTimelineEntry[];
  width?: number;
  height?: number;
}

function decisionScore(decision: SymbolTimelineEntry["decision"]): number {
  if (decision === "buy") return 1;
  if (decision === "sell") return -1;
  return 0;
}

export default function VerdictMiniChart({
  entries,
  width = 480,
  height = 120,
}: Props) {
  const usable = entries
    .map((e) => ({
      ...e,
      ts: e.finalized_at ?? e.started_at,
    }))
    .filter((e): e is SymbolTimelineEntry & { ts: string } => Boolean(e.ts))
    .map((e) => ({
      ...e,
      tsMs: new Date(e.ts).getTime(),
    }))
    .sort((a, b) => a.tsMs - b.tsMs);

  if (usable.length === 0) {
    return <p>차트로 표시할 데이터가 없습니다.</p>;
  }

  const minT = usable[0]!.tsMs;
  const maxT = usable[usable.length - 1]!.tsMs;
  const span = maxT - minT || 1;

  const padX = 16;
  const padY = 16;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;

  function xFor(tsMs: number): number {
    return padX + (innerW * (tsMs - minT)) / span;
  }
  function yFor(score: number): number {
    return padY + (innerH * (1 - score)) / 2;
  }

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label="평결 변화 미니 차트"
    >
      <line
        x1={padX}
        x2={width - padX}
        y1={yFor(0)}
        y2={yFor(0)}
        stroke="#9ca3af"
        strokeDasharray="2 2"
      />
      {usable.map((e, i) => {
        const score = decisionScore(e.decision);
        const cx = xFor(e.tsMs);
        const cy = yFor(score);
        const r = 3 + ((e.confidence ?? 0) / 100) * 5;
        return (
          <circle
            key={e.session_id}
            cx={cx}
            cy={cy}
            r={r}
            fill={
              score > 0 ? "#10b981" : score < 0 ? "#ef4444" : "#9ca3af"
            }
            opacity={0.4 + ((e.confidence ?? 0) / 100) * 0.6}
            data-index={i}
          />
        );
      })}
    </svg>
  );
}

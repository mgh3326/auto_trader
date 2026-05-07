// frontend/invest/src/components/discover/AiIssueTicker.tsx
type AiIssueTickerProps = Readonly<{
  asOf?: string | null;
  windowHours?: number;
}>;

export function AiIssueTicker({ asOf, windowHours }: AiIssueTickerProps) {
  return (
    <header style={{ marginTop: 8 }}>
      <h2 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>
        AI가 포착한 실시간 이슈
      </h2>
      <div className="subtle" style={{ marginTop: 4 }}>
        자체 수집 뉴스 클러스터링 기반입니다.
        {windowHours ? ` 최근 ${windowHours}시간` : ""}
        {asOf ? ` · 기준: ${new Date(asOf).toLocaleString("ko-KR")}` : ""}
      </div>
    </header>
  );
}

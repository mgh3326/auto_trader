// frontend/invest/src/components/discover/AiIssueTicker.tsx
type AiIssueTickerProps = Readonly<{
  asOf?: string | null;
}>;

export function AiIssueTicker({ asOf }: AiIssueTickerProps) {
  return (
    <header style={{ marginTop: 8 }}>
      <h2 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>
        AI 실시간 이슈
      </h2>
      <div className="subtle" style={{ marginTop: 4 }}>
        뉴스 기반으로 정리된 참고 정보입니다.
        {asOf ? ` 기준: ${new Date(asOf).toLocaleString("ko-KR")}` : ""}
      </div>
    </header>
  );
}

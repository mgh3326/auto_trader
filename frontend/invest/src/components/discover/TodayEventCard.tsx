// frontend/invest/src/components/discover/TodayEventCard.tsx
export function TodayEventCard() {
  return (
    <section
      aria-labelledby="today-event-heading"
      style={{
        padding: 16,
        background: "var(--surface)",
        border: "1px solid var(--surface-2)",
        borderRadius: 14,
      }}
    >
      <h2
        id="today-event-heading"
        style={{ margin: 0, fontSize: 14, fontWeight: 700 }}
      >
        오늘의 주요 이벤트
      </h2>
      <div className="subtle" style={{ marginTop: 6 }}>
        경제 캘린더는 준비 중입니다.
      </div>
      <div className="subtle" style={{ marginTop: 4 }}>
        실적/지표 일정은 후속 업데이트에서 제공됩니다.
      </div>
    </section>
  );
}

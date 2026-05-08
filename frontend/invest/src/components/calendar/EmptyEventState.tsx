import { Icon } from "../../ds";

export function EmptyEventState({ message = "이번 주는 일정이 없어요" }: { message?: string }) {
  return (
    <div
      data-testid="calendar-empty"
      style={{
        padding: "32px 16px",
        textAlign: "center",
        color: "var(--fg-3)",
        fontSize: 13,
      }}
    >
      <div
        style={{
          width: 40,
          height: 40,
          borderRadius: 999,
          background: "var(--surface-2)",
          display: "grid",
          placeItems: "center",
          margin: "0 auto 8px",
          color: "var(--fg-2)",
        }}
      >
        <Icon name="calendar" size={18} />
      </div>
      {message}
    </div>
  );
}

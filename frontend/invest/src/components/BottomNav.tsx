const TABS = ["증권", "관심", "발견", "피드"];

export function BottomNav() {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-around",
        paddingTop: 8,
        borderTop: "1px solid var(--surface-2)",
        color: "var(--muted)",
        fontSize: 10,
        position: "sticky",
        bottom: 0,
        background: "var(--bg)",
      }}
    >
      {TABS.map((label, i) => (
        <button
          key={label}
          type="button"
          onClick={() => alert("준비 중")}
          style={{
            background: "none",
            border: "none",
            color: i === 0 ? "var(--text)" : "var(--muted)",
            cursor: "pointer",
            padding: 8,
            fontSize: 10,
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

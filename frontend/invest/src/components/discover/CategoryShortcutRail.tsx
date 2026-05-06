// frontend/invest/src/components/discover/CategoryShortcutRail.tsx
const CATEGORIES = ["해외주식", "국내주식", "옵션", "채권"] as const;

export function CategoryShortcutRail() {
  return (
    <div
      role="list"
      aria-label="카테고리"
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(4, 1fr)",
        gap: 8,
      }}
    >
      {CATEGORIES.map((label) => (
        <div
          key={label}
          role="listitem"
          aria-disabled="true"
          style={{
            padding: 12,
            background: "var(--surface)",
            border: "1px solid var(--surface-2)",
            borderRadius: 12,
            color: "var(--muted)",
            fontSize: 12,
            textAlign: "center",
            opacity: 0.6,
          }}
        >
          {label}
        </div>
      ))}
    </div>
  );
}

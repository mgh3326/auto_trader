// frontend/invest/src/components/discover/CategoryShortcutRail.tsx
const CATEGORIES = ["해외주식", "국내주식", "옵션", "채권"] as const;

export function CategoryShortcutRail() {
  return (
    <ul
      aria-label="카테고리"
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(4, 1fr)",
        gap: 8,
        listStyle: "none",
        margin: 0,
        padding: 0,
      }}
    >
      {CATEGORIES.map((label) => (
        <li
          key={label}
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
        </li>
      ))}
    </ul>
  );
}

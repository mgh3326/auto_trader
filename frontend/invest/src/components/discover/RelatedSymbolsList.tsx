// frontend/invest/src/components/discover/RelatedSymbolsList.tsx
export function RelatedSymbolsList({ symbols }: { symbols: string[] }) {
  return (
    <section aria-labelledby="symbols-heading" style={{ marginTop: 16 }}>
      <h2 id="symbols-heading" style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
        관련 종목
      </h2>
      {symbols.length > 0 ? (
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: "8px 0 0",
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
          }}
        >
          {symbols.map((sym) => (
            <li
              key={sym}
              style={{
                padding: "4px 10px",
                background: "var(--surface-2)",
                color: "var(--text)",
                borderRadius: 999,
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              {sym}
            </li>
          ))}
        </ul>
      ) : (
        <div className="subtle" style={{ marginTop: 8 }}>
          관련 종목 분석은 준비 중입니다.
        </div>
      )}
    </section>
  );
}

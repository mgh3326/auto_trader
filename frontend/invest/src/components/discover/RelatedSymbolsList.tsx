// frontend/invest/src/components/discover/RelatedSymbolsList.tsx
import type { MarketIssueRelatedSymbol } from "../../types/newsIssues";

type RelatedSymbolsListProps = Readonly<{
  symbols: readonly MarketIssueRelatedSymbol[];
}>;

export function RelatedSymbolsList({ symbols }: RelatedSymbolsListProps) {
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
            flexDirection: "column",
            gap: 8,
          }}
        >
          {symbols.map((sym) => (
            <li
              key={`${sym.market}:${sym.symbol}`}
              style={{
                padding: "8px 10px",
                background: "var(--surface-2)",
                color: "var(--text)",
                borderRadius: 12,
                fontSize: 12,
                display: "flex",
                justifyContent: "space-between",
                gap: 10,
              }}
            >
              <strong>{sym.canonical_name || sym.symbol}</strong>
              <span className="subtle">
                {sym.symbol} · {sym.mention_count}회 언급
              </span>
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

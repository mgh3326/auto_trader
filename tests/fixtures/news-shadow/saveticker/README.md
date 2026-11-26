Save+ public app backend captures, labeled from `mgh3326/finance-ref-analysis`
commit `619a3dd2b5d717932b79ef8f4fa6e0a7ef1d7a35`, captured 2026-11-26.

- `real-capture-search.json`: raw `/api/search` response, including full engagement
  objects and ticker symbols.
- `real-capture-top.json`: `/api/news/top-stories` extraction from the same day;
  item fields are the extractor's reduced projection and preserve the `.KR`
  symbol suffix observed in ETF ticker objects.

These are labeled real captures, not synthetic expected-output files.

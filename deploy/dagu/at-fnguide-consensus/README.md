# at-fnguide-consensus (hk 6508)

Copy, NOT registered. The repository does not register this DAG; the design
session registers it after the PR merges and the migration
`20261125_hk6508_fnguide_snapshots` is applied.

- Source: public `wcomp.fnguide.com/CompanyInfo/{Consensus,Invest,Finance}`
  (operator decision `decision/2026-11-25/at-public-scraping-fnguide`).
- Universe: active KOSPI/KOSDAQ stocks with `security_type='STOCK'`.
- Pacing: 2 concurrent requests, about 1 request per second overall.
- Writes: insert-only, per (symbol, page, observed_date); a same-day rerun writes 0 rows.
- Manual smoke: `uv run python -m scripts.collect_fnguide_consensus --symbols 005930,000660,028300 --dry-run`.

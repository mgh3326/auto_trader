# US PIT universe — coverage and gaps (generated)

- latest observation: 2026-10-30
- `us_pit_universe.csv` sha256 `e1578f21086f4ab51ee6894fdecb82d7f9a78cada970e2b2fad3a76067f7b77c`, 32234 intervals, 29562 symbols
- inputs: 132 accepted, 0 rejected

## Inactive share

| scope | symbols | inactive | share |
|---|---:|---:|---:|
| all_symbols_all_time | 29562 | 16313 | 55.18% |
| likely_common_stock_all_time | 13419 | 8248 | 61.47% |
| all_symbols_listed_in_2016-01-01..2026-10-30 | 25474 | 12225 | 47.99% |
| likely_common_stock_listed_in_2016-01-01..2026-10-30 | 10462 | 5291 | 50.57% |
| current corpus (reference: ROB-1431 brief (survivor-only us-corpus-v1); not re-measured here) | | | 0.60% |

## Series coverage

### nasdaqlisted

- 86 observation days, 2008-01-04 → 2026-10-30
- gap between observations: median 38 days, max 681 days
- per year: 2008: 3, 2009: 1, 2010: 1, 2011: 3, 2012: 1, 2013: 2, 2014: 2, 2015: 5, 2016: 8, 2017: 5, 2018: 10, 2019: 6, 2020: 10, 2021: 4, 2022: 3, 2023: 1, 2024: 4, 2025: 7, 2026: 10
- gaps over 365 days (event dates inside are unresolved):
  - 2009-02-18 → 2010-12-31 (681 days)

### otherlisted

- 45 observation days, 2008-09-19 → 2026-10-30
- gap between observations: median 36 days, max 1323 days
- per year: 2008: 1, 2009: 2, 2010: 1, 2011: 3, 2013: 1, 2017: 5, 2018: 12, 2019: 8, 2020: 1, 2022: 2, 2024: 2, 2025: 1, 2026: 6
- gaps over 365 days (event dates inside are unresolved):
  - 2009-03-11 → 2010-09-14 (552 days)
  - 2010-09-14 → 2011-10-24 (405 days)
  - 2011-12-23 → 2013-10-23 (670 days)
  - 2013-10-23 → 2017-06-07 (1323 days)
  - 2020-01-30 → 2022-01-07 (708 days)
  - 2022-12-30 → 2024-04-26 (483 days)
  - 2024-06-14 → 2025-10-07 (480 days)

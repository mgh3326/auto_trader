# Coverage report — KR delisting / terminal outcome (ROB-1430)

Primary frame: corpus symbols with bars absent from today's KIS master (desk's 540 definition).

| Frame | Denominator | Resolved | Unresolved | Coverage |
|---|---:|---:|---:|---:|
| primary | 555 | 555 | 0 | 100.00% |
| membership_frame_not_listed | 560 | 560 | 0 | 100.00% |
| ended_in_main_scope | 404 | 404 | 0 | 100.00% |

Resolved = delist date from a public KIND/KRX source, terminal outcome in the closed vocabulary,
and (for symbols whose membership ended in 2015–2024) the date equals the next corpus session.

## Date basis (primary frame)

- kind_delist_list: 516
- kind_disclosure_body: 39

## Corpus date check (primary frame)

- equals_next_session: 397
- not_checkable_main_scope_alive: 158

## Terminal outcome (primary frame)

- dissolution_merger_or_liquidation: 36
- fund_term_expiry: 6
- merged_absorbed: 29
- preferred_conversion: 11
- preferred_float: 15
- share_exchange: 44
- spac_deadline: 117
- spac_merger: 53
- statutory_cause: 210
- voluntary: 34

## Payout semantics (primary frame; a labelling hint, not an amount)

- ambiguous: 57
- consideration_shares_or_cash: 126
- converted_to_common: 11
- residual_after_liquidation_trading: 210
- tender_or_last_price: 34
- trust_return: 117

## Unresolved symbols

None.

## Known limits

- Listing dates before 2015-01-02 are censored (`listed_before_corpus_start`); later ones are the first
  KRX membership session.
- Suspensions are the corpus' no-trade sessions (open=high=low=0, close>0); KRX halt notices are not
  collected.
- Corporate actions are those implied by the delisting reason (merger, share exchange, conversion).
  Splits and rights issues are already in the adjusted prices and are not re-derived.
- `dissolution` on KOSPI covers both absorption mergers and fund liquidations; payout stays `ambiguous`.
- Symbols delisted in 2025–2026 cannot be checked against the corpus: the holdout stays sealed.

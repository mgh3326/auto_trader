# ROB-253 /invest market insights decision

Decision: scaffold `/invest/insights` as a read-only aggregation page.

Rationale:
- ROB-249 research consensus is symbol-scoped, so it stays embedded in `/invest/stocks/:market/:symbol` where the user has a concrete stock context.
- ROB-250 common/preferred disparity and ROB-252 market parity cards are cross-market observation widgets with stronger "not a signal" caveats. Keeping them all on `/invest` or `/invest/market` would make those entry pages too dense.
- A dedicated `/invest/insights` surface lets home keep compact summaries while market insight widgets get their own loading, empty, error, and read-only guardrail copy.

Safety boundaries preserved:
- Uses existing read-only `/invest/api` frontend hooks only.
- No broker/order/watch mutation path is called or imported.
- No production DB write, backfill, or scheduler activation is added.
- No production dependency on raoni.xyz APIs is introduced.

Deferred/follow-up trigger:
- If additional cross-market insight widgets are added, place them on `/invest/insights` first and only promote compact summaries back to home after UX review.

# ROB-155 Crypto News Source Provenance Runbook

Purpose: document safe crypto source expansion expectations for `/invest/api/feed/news` without changing scheduler cadence or adding collectors in this task.

Current boundary:
- auto_trader consumes articles through existing bulk ingestion and `news_articles.feed_source` provenance.
- New external collectors, scheduler cadence changes, production backfills, and destructive cleanup require separate approval.
- This task only classifies/filters at the read layer and provides diagnostics.

Approved source candidates for a future, separately approved dry run:

| Source | Provenance key suggestion | Notes |
|---|---|---|
| CoinDesk RSS/API | `rss_coindesk` | Strong crypto source; rate limit and timeout explicitly. |
| Cointelegraph RSS | `rss_cointelegraph` | Strong crypto source; dedupe by canonical URL/title fingerprint. |
| Decrypt RSS | `rss_decrypt` | Broader tech source; apply ROB-155 broad AI/semi filter before UI prominence. |
| Bitcoin Magazine RSS | `rss_bitcoin_magazine` | Strong BTC source; avoid over-weighting BTC-only coverage. |
| Exchange announcements | `exchange_announcement_<venue>` | Public-only, rate-limited, dry-run first; no authenticated trading APIs. |

Future collector dry-run checklist:
1. Fetch source in read-only/dry-run mode and write local artifact only.
2. Record `feed_source`, canonical URL, published time, title, summary, and source-specific ID where available.
3. Validate dedupe key before DB insertion: prefer canonical URL; otherwise normalized title + source + published date bucket.
4. Apply timeout/retry/throttle limits per source.
5. Run `scripts/news_quality_baseline.py --markets crypto` and `scripts/news_issue_lab_quality_eval.py --mode tag-precision` against fixtures before production enablement.
6. Require separate approval for any scheduler, launchd, Prefect, DB backfill, or production feature-flag change.

Failure handling:
- If a source returns invalid payloads or suspicious boilerplate-only broad-tech articles, quarantine at report/runbook level first; do not hard reject existing production ingest without a follow-up plan.
- If crypto feed quality degrades, disable/demote that source in read-layer filtering before considering data mutation.

Secrets:
- Do not print API keys, cookies, tokens, DSNs, or authenticated source credentials.
- Use `[REDACTED]` placeholders in commands and Linear comments.

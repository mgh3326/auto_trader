# ROB-865 Paperclip Residue Cleanup Design

## Goal

Remove only the dead Paperclip integration inventory after ROB-864 while preserving every compatibility surface that still carries a legacy Paperclip name.

## Scope

- Remove `paperclip_api_url` and `paperclip_api_key` from runtime settings and remove the corresponding local production environment entries.
- Remove `scripts/cio_quality_gate.py`'s `load_from_paperclip` function and `--paperclip-issue` input mode.
- Preserve `x-paperclip-agent-id` as the canonical deployed MCP caller identity header. Keep the shell template's header and `PAPERCLIP_AGENT_ID` substitution variable for compatibility, and document why the legacy name remains.
- Preserve `trade_journals.paperclip_issue_id`, its index, MCP arguments, filtering behavior, and schema references. Update comments and current documentation to describe it as an external issue key whose legacy name comes from Paperclip and whose current values are Linear ROB keys.
- Do not edit archived `docs/plans/**` documents.

## Verification Design

A repository inventory test will make the acceptance boundary executable. It will reject the dead settings, environment tokens, CLI option, and loader symbol; allow remaining `paperclip` references only in the legacy-header and trade-journal compatibility surfaces; and assert that the canonical header, database column, and index remain unchanged. Existing caller-identity, MCP template, trade-journal, and ROB-864 tests will run alongside the new regression test.

## Safety

No database migration is created because the legacy column and index remain. No MCP caller header is renamed or aliased because the deployed canonical value is intentionally unchanged. The cleanup removes no active authorization mechanism; ROB-864 already replaced the former approval lookup with Telegram two-step confirmation.

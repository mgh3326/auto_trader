# Paperclip Agent Model Inventory Baseline

Generated for: ROB-279 / ROB-274 / ROB-273
Snapshot date: 2026-04-20 KST
Repository: `/home/mgh3326/auto_trader`

## Purpose

This document is the baseline inventory for the `auto_trader` Paperclip operating environment after the GPT-5.4 baseline change. It is an inventory and policy input only. It does not change any agent configuration, model setting, adapter, instruction bundle, or runtime behavior.

ROB-273 should use this as the current-state map for deciding which roles stay on GPT-5.4, which roles are Sonnet pilot candidates, and which roles need an Opus reserved or escalation lane.

## Data Sources

- Paperclip API, `GET /api/companies/{companyId}/agents`, re-fetched during the ROB-274 correction pass on 2026-04-20 KST.
- Paperclip API, per-agent `GET /api/agents/{agentId}`, re-fetched during the ROB-274 correction pass on 2026-04-20 KST.
- Paperclip API heartbeat context for ROB-273, ROB-274, and ROB-279.
- Paperclip workspace memory for Hermes adapter preference, especially `feedback_hermes_adapter.md`.
- Local managed instruction files under `/home/mgh3326/.paperclip/instances/default/companies/6a41f388-ff8d-4b65-82dc-38a9b66fa69e/agents/*/instructions/AGENTS.md`.
- Local Codex runtime config at `/home/mgh3326/.paperclip/instances/default/companies/6a41f388-ff8d-4b65-82dc-38a9b66fa69e/codex-home/config.toml`.
- Local Codex model cache at `/home/mgh3326/.paperclip/instances/default/companies/6a41f388-ff8d-4b65-82dc-38a9b66fa69e/codex-home/models_cache.json`.

## Evidence Framing

- The current company agent API returned 14 company agent records. Thirteen are active for this inventory because their status is `idle` or `running`: CEO, CTO, CIO, Scout, Trader, Trading Coordinator, Investment Reviewer, Market Intelligence Analyst, Staff Engineer, Software Engineer, Code Reviewer, Release Engineer, and QA Engineer.
- `PR Triage Reviewer` is present in the current API snapshot, but its status is `pending_approval` and it has no heartbeat yet. It is documented separately as pending, not counted in active current-state inventory.
- Active Codex agents have current API `adapterType = "codex_local"`. The shared local Codex runtime config sets `model = "gpt-5.4"` and the current Staff Engineer per-agent API record exposes `adapterConfig.model = "gpt-5.4"`. For this baseline document, active Codex agents are therefore documented as current-state `codex_local / gpt-5.4` unless a stronger per-agent override appears later.
- Trading Coordinator has current API `adapterType = "hermes_local"`. Paperclip workspace memory explicitly records the board preference that Trading Coordinator uses `hermes_local` with `gpt-5.4`. Its current-state row therefore uses `hermes_local / gpt-5.4`, while noting that the agent API itself exposes the adapter but not a model field.
- Current API records are treated as source of truth for current existence, status, adapter type, reporting line, and capability text. Local instruction files are used for responsibility detail and historical/stale model evidence only when the API does not expose that text directly.
- Several local instruction files contain stale model/adapter references from before the GPT-5.4 baseline change. Those conflicts are documented separately from current-state evidence so they do not make active API state look ambiguous.
- A local KR Scout instruction directory exists, but KR Scout does not appear in the current company agent API list. It remains inactive/stale and is not part of active current-state inventory.
- Remaining true ambiguity is narrow: most non-current-agent API reads redact per-agent `adapterConfig`, and the Hermes adapter does not expose a current model field through the agent API. If Paperclip later exposes non-secret model names in the company agent API, this document should be refreshed from that direct source.

## Tag Definitions

- `keep_on_gpt54`: routine or bounded work where GPT-5.4 is adequate with normal review, or where the role exists specifically to provide a GPT-5.4 perspective.
- `candidate_for_sonnet`: quality-sensitive synthesis, coordination, or review work where a mid-tier stronger model may reduce rework but Opus is not the default.
- `candidate_for_opus`: high-failure-cost strategy, final investment judgment, execution sign-off, architecture/security/release decisions, or work that was previously explicitly framed as Opus-family.

## Active Agent Inventory

| Agent | Role / title | Reports to | Current state evidence | Operational responsibility | Historical / stale instruction evidence | Risk | Candidate tag |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CEO (`f616b715`) | `ceo`, CEO | None | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Owns company strategy, prioritization, cross-functional routing, approvals, and policy synthesis. Delegates technical work to CTO and investment work to CIO. | Not directly documented as Opus. Role is still highest-leverage policy owner. | High: final policy decisions and exception handling can affect trading governance and agent structure. | `candidate_for_opus` for final policy approval and high-risk exceptions; routine routing can stay GPT-5.4. |
| CTO (`98bb1bf1`) | `cto`, Chief Technology Officer | CEO | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Owns technical quality, architecture, PR workflow, branch strategy, code review oversight, Docker/deploy governance, and technical delegation. | Local CTO instructions contain outdated report table with Staff `o4-mini`, Release `gemini-2.5-pro`, QA `gemini-2.5-flash`; API now shows these reports as active `codex_local`. | High: architecture, security, migrations, and merge/release decisions have broad blast radius. | `candidate_for_opus` for architecture/security/migration/release approval; `candidate_for_sonnet` for normal planning/review management. |
| CIO (`14a97c4a`) | `general`, Chief Investment Officer | CEO | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Daily investment decision owner. Directs Scout, decides when Investment Reviewer is required, checks Trading Coordinator output, and signs off Trader execution. | Local CIO instructions explicitly describe Scout and Trader as Opus and the older investment chain as Opus-heavy with GPT-5.4 challenger review. | Critical: final investment judgment and execution sign-off can lead to live trading losses. | `candidate_for_opus` for final trade/investment decisions and policy exceptions. |
| Scout (`76e88bf1`) | `general`, Multi-Market Strategy Scout | CIO | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Cross-market strategy exploration across KR, US, and crypto. Read-only analysis, opportunity discovery, position validation, and Scout Reports for CIO. | CIO instructions identify Scout as Opus. Investment Reviewer instructions describe the investment chain as Opus-dominant. | High: poor analysis can drive bad CIO decisions, but Scout cannot place orders. | `candidate_for_opus` for new position theses, concentrated-position changes, and major strategy shifts; routine screens can stay GPT-5.4 or be Sonnet-canary tested. |
| Trader (`6b2192cc`) | `general`, Multi-Market Trading Operator | CIO | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Real-time KR/US/crypto trading operations, portfolio lookups, dry-run order creation, chart checks, trade journal queries, and approved execution handoff. | CIO instructions identify Trader as Opus. Trading workflow treats Trader as final execution operator after board/CIO approval. | Critical: live order execution and portfolio state changes have direct financial impact. | `candidate_for_opus` for live execution and order sizing checks; keep dry-run/reporting on GPT-5.4 with guardrails. |
| Trading Coordinator (`e60d5912`) | `general`, Trading Coordinator | CIO | Current API: active `hermes_local`; Paperclip Hermes memory: `gpt-5.4` | Converts CIO final decisions into board-ready trading briefings, manages approval gates, and hands approved trades to Trader. Current API status is `idle`. | CIO instructions list Trading Coordinator as Hermes, not Opus. Earlier ROB-279 status instability (`error`) is historical and no longer current state. | High: briefing errors can cause board misapproval or wrong Trader handoff, but it should not analyze charts or execute orders. | `keep_on_gpt54` for routine briefing and gate tracking through Hermes; `candidate_for_sonnet` only if ROB-273 later moves the role out of Hermes or adds stronger synthesis review. |
| Investment Reviewer (`c9e874ff`) | `general`, Investment Reviewer | CIO | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Independent thesis critique and risk check before CIO decisions. Mandatory for hard gates such as new positions, concentrated positions, sector changes, and counter-thesis trades. | Local instructions explicitly say this role runs on GPT-5.4 to add non-Opus cross-model diversity to an Opus-dominant chain. | High: review misses can leave CIO without needed challenge; role has no veto or execution authority. | `keep_on_gpt54` by design for model diversity; add stronger-model escalation only when reviewer finds unresolved high-risk ambiguity. |
| Market Intelligence Analyst (`49de138d`) | `general`, Market Intelligence Analyst | CIO | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Structures long-form news, filings, transcripts, macro/policy context, event memos, catalyst maps, and briefing context blocks for CIO/Scout/Reviewer/Trading Coordinator. | Historical/stale local instructions say `claude_local`, unpinned default Claude, and Opus 4.7-family pilot. That conflicts with current API adapter state and should be handled as stale instruction cleanup, not active model ambiguity. | Medium-high: misinformation in catalyst context can pollute investment decisions, but MIA does not decide trades. | `candidate_for_sonnet` for cost/quality pilot. Separate Opus escalation for MIA would be a future ROB-273 policy proposal requiring instruction changes, not current-state inventory. |
| Staff Engineer (`f4f81bbe`) | `engineer`, Staff Engineer | CTO | Current API: active `codex_local`; direct per-agent API model: `gpt-5.4` | Hands-on feature development, bug fixes, refactoring, implementation planning, and delegation to Software Engineer / Code Reviewer / QA where needed. | No current Opus-based instruction evidence. CTO instructions had outdated Staff model `o4-mini`, superseded by API adapter evidence and the current GPT-5.4 baseline. | Medium-high: changes can affect trading runtime, MCP, auth, deploy, or data paths. | `keep_on_gpt54` for routine implementation; `candidate_for_opus` escalation for security/auth, migrations, architecture, live trading write paths, or large refactors. |
| Software Engineer (`a4af3319`) | `engineer`, Software Engineer | Staff Engineer | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Python backend implementation for APIs, services, websocket handlers, MCP tools, n8n workflow code, features, and bug fixes delegated by Staff Engineer. | No current Opus-based instruction evidence. | Medium: primary authoring risk, mitigated by Staff/Code Reviewer/QA gates. | `keep_on_gpt54` for bounded tasks; escalate to Staff/Opus lane for high-risk modules. |
| Code Reviewer (`8b0c84c7`) | `engineer`, Code Reviewer | Staff Engineer | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Final code quality gate for auto_trader PRs. Reviews scope, correctness, tests, edge cases, style, and risk; consumes PR Triage Reviewer artifacts where present. | Local instructions mention a GPT-5.4 PR Triage Reviewer; current API now has that record, but it is `pending_approval`, not active. | High: review misses can let regressions through; final approval authority creates leverage. | `candidate_for_sonnet` for normal review quality uplift; `candidate_for_opus` escalation for security/auth, migrations, live trading, or broad architectural changes. |
| Release Engineer (`19cb1efc`) | `engineer`, Release Engineer | CTO | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Docker builds, deployment pipelines, production releases, and infrastructure changes. | CTO instructions had outdated Release `gemini_local / gemini-2.5-pro`; API now shows active `codex_local`. | High: deploy errors can affect production availability and trading operations. | `candidate_for_sonnet` for release planning/checklists; `candidate_for_opus` escalation for production rollback, migration, auth/secret, or infrastructure changes with high blast radius. |
| QA Engineer (`192e62cd`) | `engineer`, QA Engineer | CTO | Current API: active `codex_local`; current Codex baseline: `gpt-5.4` | Test writing, test execution, quality validation, regression checks, and independent validation of engineering outputs. | CTO instructions had outdated QA `gemini_local / gemini-2.5-flash`; API now shows active `codex_local`. | Medium: QA misses reduce confidence but normally do not directly change production. | `keep_on_gpt54` for routine regression and completeness checks; `candidate_for_sonnet` for exploratory QA, risk analysis, and canary evaluation. |

## Pending / Non-Active Records

| Record | Current API status | Evidence | Policy impact |
| --- | --- | --- | --- |
| PR Triage Reviewer (`956e799a`) | `pending_approval` | Current API returns the agent with `adapterType = "codex_local"`, reports-to CTO, no heartbeat yet, and read-only first-pass PR triage capabilities. | Not part of active inventory until approved. If activated on the current Codex baseline, classify as `keep_on_gpt54` for first-pass triage only, with Code Reviewer retaining final review authority. |
| KR Scout (`c7541af2`) | Not returned by current company agent API list | Local instructions exist under the company instance and describe a dedicated Korean stock strategy explorer reporting through CEO-era workflow. | Treat as inactive or stale until Paperclip API confirms reactivation. Do not include it in active lane assignments. If restored, classify similarly to Scout: read-only analysis can start GPT-5.4/Sonnet, new trade theses need Opus escalation. |

## Immediate Risk Notes

- Trading Coordinator is currently `hermes_local / gpt-5.4` for this inventory, but the agent API exposes only `hermes_local`; it does not expose a Hermes model field. Future API visibility would make this fully direct.
- Market Intelligence Analyst current state is `codex_local / gpt-5.4` from the current API plus Codex baseline. Its local `claude_local` / Opus 4.7-family text is stale instruction evidence and should be cleaned up before a Sonnet pilot uses that file as policy text.
- Investment-side instructions still describe an Opus-dominant chain while current API/adapters and ROB-273 baseline indicate GPT-5.4 current state. ROB-273 policy should decide which references become explicit escalation requirements and which are legacy assumptions.
- CTO instructions include stale model/adapter rows for Staff, Release, and QA. API records show all three are currently active `codex_local`; the stale table should be corrected by ROB-275 guardrail work.
- `PR Triage Reviewer` exists in current API but is `pending_approval`. It should not be used as an active review lane until approved and started.
- Because most non-current-agent model fields are redacted, a final authoritative inventory would be stronger if Paperclip exposed non-secret model names through the company agent API or provided a controlled export from the agent configuration store.

## Baseline Summary For ROB-273

| Lane | Agents / work types |
| --- | --- |
| `keep_on_gpt54` | Staff Engineer routine implementation, Software Engineer bounded implementation, QA routine regression, Investment Reviewer challenger role, Trading Coordinator routine briefing through Hermes, PR Triage Reviewer first-pass triage if approved, CEO/CTO routine routing and issue decomposition. |
| `candidate_for_sonnet` | Code Reviewer normal PR reviews, Release Engineer normal release planning, QA exploratory validation, Market Intelligence Analyst routine catalyst/context synthesis, Trading Coordinator briefing synthesis only if moved out of Hermes or given extra stronger-review duties. |
| `candidate_for_opus` | CEO final policy approval, CTO architecture/security/migration/release approval, CIO final investment decisions, Scout new thesis or major rebalance reports, Trader live execution and order sizing, Code Reviewer reviews for high-blast-radius code, Release Engineer production incident/rollback decisions. |

Future policy note: a future ROB-273 policy could add MIA ambiguous market-moving source synthesis to the Opus lane, but that would require updating MIA instructions because the current local boundary reserves the separate Opus escalation path for Scout/Trader/CIO sign-off.

## Recommended Next Steps

1. ROB-280 should validate this inventory against the Paperclip UI or another non-redacted configuration source.
2. ROB-275 should update stale instruction references so they no longer contradict the API baseline.
3. ROB-276 should define exact investment/trading stronger-model gates for CIO, Scout, Trader, Trading Coordinator, and Investment Reviewer, and explicitly decide whether MIA remains outside the separate Opus escalation path or receives an instruction-changing ROB-273 policy update.
4. ROB-278 should use this inventory to select a narrow Sonnet pilot set: Code Reviewer normal PRs, QA exploratory validation, Release normal release planning, and MIA routine catalyst briefs.

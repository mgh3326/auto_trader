# Paperclip Agent Model Inventory Baseline

Generated for: ROB-279 / ROB-274 / ROB-273
Snapshot date: 2026-04-20 KST
Repository: `/home/mgh3326/auto_trader`

## Purpose

This document is the baseline inventory for the `auto_trader` Paperclip operating environment after the GPT-5.4 baseline change. It is an inventory and policy input only. It does not change any agent configuration, model setting, adapter, instruction bundle, or runtime behavior.

ROB-273 should use this as the current-state map for deciding which roles stay on GPT-5.4, which roles are Sonnet pilot candidates, and which roles need an Opus reserved or escalation lane.

## Data Sources

- Paperclip API, `GET /api/companies/{companyId}/agents`, fetched during ROB-279 execution.
- Paperclip API, per-agent `GET /api/agents/{agentId}`, fetched during ROB-279 execution.
- Paperclip API heartbeat context for ROB-273, ROB-274, and ROB-279.
- Local managed instruction files under `/home/mgh3326/.paperclip/instances/default/companies/6a41f388-ff8d-4b65-82dc-38a9b66fa69e/agents/*/instructions/AGENTS.md`.
- Local Codex runtime config at `/home/mgh3326/.paperclip/instances/default/companies/6a41f388-ff8d-4b65-82dc-38a9b66fa69e/codex-home/config.toml`.
- Local Codex model cache at `/home/mgh3326/.paperclip/instances/default/companies/6a41f388-ff8d-4b65-82dc-38a9b66fa69e/codex-home/models_cache.json`.

## Evidence Limits

- The current company agent API returned 13 active agents during resume verification: CEO, CTO, CIO, Scout, Trader, Trading Coordinator, Investment Reviewer, Market Intelligence Analyst, Staff Engineer, Software Engineer, Code Reviewer, Release Engineer, and QA Engineer.
- The company agent-list API redacts most `adapterConfig` fields. Per-agent API reads also returned `{}` for most agents' adapter configs. The QA Engineer record was the only record that exposed a concrete Codex model field, `adapterConfig.model = "gpt-5.4"`.
- The local Codex config sets default `model = "gpt-5.4"` and `model_reasoning_effort = "medium"`. For active `codex_local` agents whose API model field is redacted, this document marks the current model as `gpt-5.4 inferred`, not directly observed.
- ROB-273 and ROB-274 explicitly define the operating premise as "current agents are on GPT-5.4 baseline." This document uses that premise for lane tagging, while still preserving the evidence distinction above.
- Several local instruction files contain stale model/adapter references from before the baseline change. The active Paperclip API record is treated as the current source for agent existence, adapter type, reporting line, and status; local instructions are used for responsibility and prior Opus-based-role inference.
- Trading Coordinator status changed during ROB-279 work: an earlier API read returned `error`, while resume verification returned `running`. The adapter remains `hermes_local`, and the underlying Hermes model was still not exposed by available API fields.
- A local KR Scout instruction directory exists, but the KR Scout agent does not appear in the current company agent API list. It is listed separately as a local-only/stale record, not as an active agent.

## Tag Definitions

- `keep_on_gpt54`: routine or bounded work where GPT-5.4 is adequate with normal review, or where the role exists specifically to provide a GPT-5.4 perspective.
- `candidate_for_sonnet`: quality-sensitive synthesis, coordination, or review work where a mid-tier stronger model may reduce rework but Opus is not the default.
- `candidate_for_opus`: high-failure-cost strategy, final investment judgment, execution sign-off, architecture/security/release decisions, or work that was previously explicitly framed as Opus-family.

## Active Agent Inventory

| Agent | Role / title | Reports to | Adapter | Current model evidence | Operational responsibility | Prior Opus-based-role inference | Risk | Candidate tag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| CEO (`f616b715`) | `ceo`, CEO | None | `codex_local` | `gpt-5.4 inferred` from Codex default + ROB-273/274 premise; API model redacted | Owns company strategy, prioritization, cross-functional routing, approvals, and policy synthesis. Delegates technical work to CTO and investment work to CIO. | Not directly documented as Opus. Role is still highest-leverage policy owner. | High: final policy decisions and exception handling can affect trading governance and agent structure. | `candidate_for_opus` for final policy approval and high-risk exceptions; routine routing can stay GPT-5.4. |
| CTO (`98bb1bf1`) | `cto`, Chief Technology Officer | CEO | `codex_local` | `gpt-5.4 inferred`; API model redacted | Owns technical quality, architecture, PR workflow, branch strategy, code review oversight, Docker/deploy governance, and technical delegation. | Local CTO instructions contain outdated report table with Staff `o4-mini`, Release `gemini-2.5-pro`, QA `gemini-2.5-flash`; API now shows these reports as `codex_local`. | High: architecture, security, migrations, and merge/release decisions have broad blast radius. | `candidate_for_opus` for architecture/security/migration/release approval; `candidate_for_sonnet` for normal planning/review management. |
| CIO (`14a97c4a`) | `general`, Chief Investment Officer | CEO | `codex_local` | `gpt-5.4 inferred`; API model redacted | Daily investment decision owner. Directs Scout, decides when Investment Reviewer is required, checks Trading Coordinator output, and signs off Trader execution. | Local CIO instructions explicitly describe Scout and Trader as Opus and the overall investment chain as Opus-heavy with GPT-5.4 challenger review. | Critical: final investment judgment and execution sign-off can lead to live trading losses. | `candidate_for_opus` for final trade/investment decisions and policy exceptions. |
| Scout (`76e88bf1`) | `general`, Multi-Market Strategy Scout | CIO | `codex_local` | `gpt-5.4 inferred`; API model redacted | Cross-market strategy exploration across KR, US, and crypto. Read-only analysis, opportunity discovery, position validation, and Scout Reports for CIO. | CIO instructions identify Scout as Opus. Investment Reviewer instructions describe the investment chain as Opus-dominant. | High: poor analysis can drive bad CIO decisions, but Scout cannot place orders. | `candidate_for_opus` for new position theses, concentrated-position changes, and major strategy shifts; routine screens can stay GPT-5.4 or be Sonnet-canary tested. |
| Trader (`6b2192cc`) | `general`, Multi-Market Trading Operator | CIO | `codex_local` | `gpt-5.4 inferred`; API model redacted | Real-time KR/US/crypto trading operations, portfolio lookups, dry-run order creation, chart checks, trade journal queries, and approved execution handoff. | CIO instructions identify Trader as Opus. Trading workflow treats Trader as final execution operator after board/CIO approval. | Critical: live order execution and portfolio state changes have direct financial impact. | `candidate_for_opus` for live execution and order sizing checks; keep dry-run/reporting on GPT-5.4 with guardrails. |
| Trading Coordinator (`e60d5912`) | `general`, Trading Coordinator | CIO | `hermes_local` | Hermes model not exposed by API; not classifiable as GPT-5.4/Sonnet/Opus from available fields | Converts CIO final decisions into board-ready trading briefings, manages approval gates, and hands approved trades to Trader. Current resume verification status is `running`; earlier ROB-279 fetch observed `error`. | CIO instructions list Trading Coordinator as Hermes, not Opus. | High: briefing errors can cause board misapproval or wrong Trader handoff, but it should not analyze charts or execute orders. | `candidate_for_sonnet` if moved into a GPT/Sonnet/Opus policy lane; first resolve Hermes model visibility. |
| Investment Reviewer (`c9e874ff`) | `general`, Investment Reviewer | CIO | `codex_local` | `gpt-5.4 inferred`; API model redacted | Independent thesis critique and risk check before CIO decisions. Mandatory for hard gates such as new positions, concentrated positions, sector changes, and counter-thesis trades. | Local instructions explicitly say this role runs on GPT-5.4 to add non-Opus cross-model diversity to an Opus-dominant chain. | High: review misses can leave CIO without needed challenge; role has no veto or execution authority. | `keep_on_gpt54` by design for model diversity; add stronger-model escalation only when reviewer finds unresolved high-risk ambiguity. |
| Market Intelligence Analyst (`49de138d`) | `general`, Market Intelligence Analyst | CIO | API: `codex_local`; local instructions: `claude_local` | API adapter says `codex_local`; current model redacted. Local instructions say unpinned default Claude, currently Opus 4.7 family. This is the largest evidence conflict. | Structures long-form news, filings, transcripts, macro/policy context, event memos, catalyst maps, and briefing context blocks for CIO/Scout/Reviewer/Trading Coordinator. | Local instructions explicitly describe an Opus-family pilot and a later Sonnet transition decision, but also state MIA does not call the separate Opus escalation path reserved for Scout/Trader/CIO sign-off. | Medium-high: misinformation in catalyst context can pollute investment decisions, but MIA does not decide trades. | `candidate_for_sonnet` for cost/quality pilot. Separate Opus escalation for MIA would be a future ROB-273 policy proposal requiring instruction changes, not current-state inventory. |
| Staff Engineer (`f4f81bbe`) | `engineer`, Staff Engineer | CTO | `codex_local` | `gpt-5.4 inferred` from Codex default + ROB-273/274 premise; API model redacted in QA per-agent reads | Hands-on feature development, bug fixes, refactoring, implementation planning, and delegation to Software Engineer / Code Reviewer / QA where needed. | No current Opus-based instruction evidence. CTO instructions had outdated Staff model `o4-mini`, superseded by API adapter evidence and the current GPT-5.4 baseline premise. | Medium-high: changes can affect trading runtime, MCP, auth, deploy, or data paths. | `keep_on_gpt54` for routine implementation; `candidate_for_opus` escalation for security/auth, migrations, architecture, live trading write paths, or large refactors. |
| Software Engineer (`a4af3319`) | `engineer`, Software Engineer | Staff Engineer | `codex_local` | `gpt-5.4 inferred`; API model redacted | Python backend implementation for APIs, services, websocket handlers, MCP tools, n8n workflow code, features, and bug fixes delegated by Staff Engineer. | No current Opus-based instruction evidence. | Medium: primary authoring risk, mitigated by Staff/Code Reviewer/QA gates. | `keep_on_gpt54` for bounded tasks; escalate to Staff/Opus lane for high-risk modules. |
| Code Reviewer (`8b0c84c7`) | `engineer`, Code Reviewer | Staff Engineer | `codex_local` | `gpt-5.4 inferred`; API model redacted | Final code quality gate for auto_trader PRs. Reviews scope, correctness, tests, edge cases, style, and risk; consumes PR Triage Reviewer artifacts where present. | Local instructions mention a GPT-5.4 PR Triage Reviewer, but no active PR Triage Reviewer agent appears in the API list. | High: review misses can let regressions through; final approval authority creates leverage. | `candidate_for_sonnet` for normal review quality uplift; `candidate_for_opus` escalation for security/auth, migrations, live trading, or broad architectural changes. |
| Release Engineer (`19cb1efc`) | `engineer`, Release Engineer | CTO | `codex_local` | `gpt-5.4 inferred`; API model redacted | Docker builds, deployment pipelines, production releases, and infrastructure changes. | CTO instructions had outdated Release `gemini_local / gemini-2.5-pro`; API now shows `codex_local`. | High: deploy errors can affect production availability and trading operations. | `candidate_for_sonnet` for release planning/checklists; `candidate_for_opus` escalation for production rollback, migration, auth/secret, or infrastructure changes with high blast radius. |
| QA Engineer (`192e62cd`) | `engineer`, QA Engineer | CTO | `codex_local` | Direct API evidence: `adapterConfig.model = "gpt-5.4"` | Test writing, test execution, quality validation, regression checks, and independent validation of engineering outputs. | CTO instructions had outdated QA `gemini_local / gemini-2.5-flash`; API now shows `codex_local` and the per-agent API exposed the current GPT-5.4 model. | Medium: QA misses reduce confidence but normally do not directly change production. | `keep_on_gpt54` for routine regression and completeness checks; `candidate_for_sonnet` for exploratory QA, risk analysis, and canary evaluation. |

## Local-Only / Stale Records

| Local record | Current API status | Evidence | Policy impact |
| --- | --- | --- | --- |
| KR Scout (`c7541af2`) | Not returned by current company agent API list | Local instructions exist under the company instance and describe a dedicated Korean stock strategy explorer reporting through CEO-era workflow. | Treat as inactive or stale until Paperclip API confirms reactivation. Do not include it in active lane assignments. If restored, classify similarly to Scout: read-only analysis can start GPT-5.4/Sonnet, new trade theses need Opus escalation. |
| PR Triage Reviewer | Not returned by current company agent API list | Code Reviewer instructions mention a GPT-5.4 PR Triage Reviewer artifact path from ROB-186. | Treat as a historical or future lane, not an active agent. If recreated, likely `keep_on_gpt54` for first-pass triage only. |

## Immediate Risk Notes

- Trading Coordinator runtime status changed from `error` to `running` during ROB-279 work; ROB-273 policy should not rely on TC model tier until Hermes model visibility is clarified.
- Market Intelligence Analyst has a direct conflict between API adapter (`codex_local`) and local instructions (`claude_local`, Opus 4.7 family). This should be fixed or explicitly accepted before any Sonnet pilot.
- Investment-side instructions still describe an Opus-dominant chain while the current API/adapters and ROB-273 premise indicate GPT-5.4 baseline. The policy must decide whether those references are legacy assumptions or active escalation requirements.
- CTO instructions include stale model/adapter rows for Staff, Release, and QA. API records show all three are currently `codex_local`; the stale table should be corrected by ROB-275 guardrail work.
- Because most model fields are redacted, a final authoritative inventory should either expose non-secret model names through Paperclip API or include a controlled export from the company agent configuration store.

## Baseline Summary For ROB-273

| Lane | Agents / work types |
| --- | --- |
| `keep_on_gpt54` | Staff Engineer routine implementation, Software Engineer bounded implementation, QA routine regression, Investment Reviewer challenger role, CEO/CTO routine routing and issue decomposition. |
| `candidate_for_sonnet` | Code Reviewer normal PR reviews, Release Engineer normal release planning, QA exploratory validation, Trading Coordinator briefing synthesis if moved from Hermes, Market Intelligence Analyst routine catalyst/context synthesis. |
| `candidate_for_opus` | CEO final policy approval, CTO architecture/security/migration/release approval, CIO final investment decisions, Scout new thesis or major rebalance reports, Trader live execution and order sizing, Code Reviewer reviews for high-blast-radius code, Release Engineer production incident/rollback decisions. A future ROB-273 policy could add MIA ambiguous market-moving source synthesis to this lane, but that would require updating MIA instructions because the current local boundary reserves the separate Opus escalation path for Scout/Trader/CIO sign-off. |

## Recommended Next Steps

1. ROB-280 should validate this inventory against the Paperclip UI or another non-redacted configuration source.
2. ROB-275 should update stale instruction references so they no longer contradict the API baseline.
3. ROB-276 should define exact investment/trading stronger-model gates for CIO, Scout, Trader, Trading Coordinator, and Investment Reviewer, and explicitly decide whether MIA remains outside the separate Opus escalation path or receives an instruction-changing ROB-273 policy update.
4. ROB-278 should use this inventory to select a narrow Sonnet pilot set: Code Reviewer normal PRs, QA exploratory validation, Release normal release planning, and MIA routine catalyst briefs.

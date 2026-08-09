# DFC_V22_RESEARCH_MIN — A2 contract

Status: signed **before** any data contact. Registered as a new ID/SHA per NW-F2.
Scope: documentation, schema and golden fixtures only. This contract performs no
collection, no listing, no download and no backtest.

| | |
|---|---|
| Contract ID | `DFC_V22_RESEARCH_MIN` |
| Judged dimensions | kline OFI · premium-index · PIT universe · outcome evidence |
| Upstream wording (canonical) | `~/work/herdr-inbox/answer-codexmock-next-wave-1630.md`, sha256 `df7aee908e50af42…` (137 lines) |
| Binding record | `~/work/herdr-inbox/operator-decisions-20260805-0830.md` §25차 |
| Schema | `research/dfc_v22_research_min/schema.py` |
| Validators | `research/dfc_v22_research_min/validation.py` |
| Golden cases | `tests/research/dfc_v22_research_min/golden/violation_cases.json` |

The four upstream clauses are reproduced below **verbatim**, in the language they
were signed in. They are also carried byte-for-byte in
`research/dfc_v22_research_min/nw_verbatim.py`, and a test asserts that this
document and that module agree, so no later edit can quietly paraphrase a clause.
Everything outside the quote blocks is transcription into schema — it adds
enforcement, never scope.

## Why this contract exists (A2 vs A1)

FUT-DATA-A1 is final at `UNDETERMINED`. It is a readiness statement about a wider
futures research corpus — funding, open interest, mark and index — and DFC-v2.2
signals read none of those: they read 4h kline OFI and the premium index. Making
A1 the gate for DFC would gate the work on evidence the strategy never touches,
in both directions. A1 is therefore preserved as-is and A2 is registered
separately. This is not a post-hoc relaxation: zero backtests have been run, and
the numbers being frozen here are frozen blind.

---

## A2-C1 — Scope separation (NW-F2)

> **NW-F2 — A1과 DFC 최소 corpus의 범위 분리** (원문 축자 인용)
>
> **“A1의 funding/OI/mark/index 종합 readiness는 보존하되 DFC-v2.x의 선행조건으로 쓰지 않는다. 데이터 접촉 전에 `DFC_V22_RESEARCH_MIN` A2 계약을 새 ID/SHA로 등록한다. A2는 kline OFI·premium-index·PIT universe·outcome evidence만 판정한다.”** 이는 결과를 본 뒤 게이트를 완화하는 것이 아니라, 아직 백테스트 0회인 상태에서 계약-데이터 불일치를 교정하는 것이다.


**Enforced as.** `validate_manifest` requires
`prerequisite_readiness.contract_id == "DFC_V22_RESEARCH_MIN"` and
`prerequisite_readiness.dimensions` to equal exactly
`("kline_ofi", "premium_index", "pit_universe", "outcome_evidence")`. Naming
FUT-DATA-A1 as the prerequisite raises `RUN_INVALID_SCOPE_SEPARATION`. A1's own
record is untouched by this contract.

---

## A2-C2 / A2-C3 — Corpus freeze literals and source material (NW-F5)

> **NW-F5 — corpus 동결 리터럴** (원문 축자 인용)
>
> **“corpus ID=`dfc-2c-4h-v22-corpus-v1`, root=`/Users/mgh3326/work/herdr-artifacts/dfc-2c-4h-v22-corpus-v1/`; warmup `[2021-02-02T00:00Z,2021-05-02T00:00Z)`, 판정창 `[2021-05-02T00:00Z,2023-08-04T00:00Z)`, 마지막 outcome용 다음 4h bar 포함. 매 epoch 직전 30 calendar day quote-volume으로 당시 eligible USD-M perpetual 전수를 순위화해 top 3, 동률은 canonical symbol 오름차순. 필수 원문은 Binance USD-M 4h kline 12필드와 premiumIndex 4h close, contract lifecycle/eligibility evidence다. funding/OI/mark/index는 이 corpus에 넣지 않는다. imputation 0.”**


**Enforced as.** `contract.py` freezes each literal, and `validate_manifest`
compares them for equality — not for containment, not for "at least":

| Literal | Frozen value |
|---|---|
| `corpus_id` | `dfc-2c-4h-v22-corpus-v1` |
| `root` | `/Users/mgh3326/work/herdr-artifacts/dfc-2c-4h-v22-corpus-v1/` |
| warmup | `[2021-02-02T00:00:00Z, 2021-05-02T00:00:00Z)` |
| judgment window | `[2021-05-02T00:00:00Z, 2023-08-04T00:00:00Z)` |
| outcome tail | exactly 1 further completed 4h bar |
| universe | rank all then-eligible USD-M perpetuals by the preceding 30 calendar days of quote volume, take top 3, ties broken by canonical symbol ascending |
| required source material | Binance USD-M 4h kline (all 12 fields) · premiumIndex 4h close · contract lifecycle/eligibility evidence |
| excluded source material | funding · open interest · mark price · index price |
| imputation | `policy = "forbidden"`, `imputed_row_count = 0` |

A scratch path is not the corpus: `root` must match the frozen literal, so a
`/private/tmp` build cannot be presented as the frozen artifact.

Exclusion is checked twice, because a forbidden source can arrive either as a
declared manifest source or as a column smuggled into a canonical table. A
`kind` in the forbidden set, and any column or endpoint whose name contains
`funding`, `open_interest`, `mark_price` or `index_price`, both raise
`RUN_INVALID_FORBIDDEN_SOURCE`.

The canonical tables are **closed schemas** — exact column set, exact order,
exact types, no extra columns. Decimal fields (`open`/`high`/`low`/`close`/
volumes/`premium_index_close`) are typed `string` and carry the raw payload token
unchanged, because A2-C5 admissibility is decided by comparing raw payload
hashes and a float round-trip destroys precisely those bytes.

---

## A2-C4 — Outcome semantics (NW-F4)

> **NW-F4 — outcome 의미론** (원문 축자 인용)
>
> **“signal epoch t의 `BasketDecision.candidate_any`가 arm label, 같은 decision의 winner가 심볼이다. outcome은 그 심볼의 완결된 t kline close부터 즉시 다음 완결 4h kline close까지의 absolute log return bps다. 둘 모두 raw evidence에서만 생성한다. 다음 bar가 없거나 불완전하면 행을 임의 삭제하지 않고 `RUN_INVALID_OUTCOME_EVIDENCE`; 마지막 signal을 위해 corpus에는 한 개의 다음 4h bar를 추가한다. 이는 PnL/체결 가능성 주장이 아니다.”** 자유 bool·자유 가격 입력을 금지한다.


**Enforced as.** `validate_outcomes(outcomes, klines, decisions)` takes the
decision record as a separate input, and:

1. **Closed outcome schema.** Any column outside the eleven declared ones raises
   `RUN_INVALID_OUTCOME_EVIDENCE`. A boolean-typed extra column and a
   price/PnL-named extra column are reported with their own wording, because
   those are the two shapes the forbidden free inputs actually take.
2. **No deletion.** Every `signal_epoch_open_time` in `decisions` must have
   exactly one outcome row. A shorter outcome table is the deletion this clause
   forbids, and it fails as `RUN_INVALID_OUTCOME_EVIDENCE` — never as a silent
   pass on the surviving rows.
3. **Next bar is the next bar.** `next_kline_open_time` must equal
   `t_kline_open_time + 4h`, and that bar must be present in `klines_4h` with a
   matching `close_time`. Absent or mismatched ⇒ `RUN_INVALID_OUTCOME_EVIDENCE`.
4. **Raw evidence only.** The arm label and the winner must equal the decision's
   `candidate_any` and winner; both referenced closes must match the
   `payload_sha256` of the raw bars they claim to come from; and
   `outcome_abs_log_return_bps` is recomputed from those two raw close tokens and
   compared within 1e-6 bps. A hand-written outcome number cannot survive this.
5. **Closed arm-label domain.** `candidate_any` is a *label*, and the label set
   is exactly two values — `candidate` and `control`, in that order. The wire
   type is `string`, so the type alone cannot pin the domain; the validator
   does, on both the decision side and the row side, before any comparison.

### The arm label (`candidate_any`)

NW-F4 says `BasketDecision.candidate_any` **is an arm label**. It is therefore
carried as a label and not as a truth value, and its domain is closed:

| | |
|---|---|
| Admissible values | `candidate`, `control` — exactly these two, in this order |
| Wire type | `string` (a label is a name; `pa.bool_()` would re-introduce the free bool NW-F4 forbids) |
| Counterpart declaration | `research_contracts/dfc_2c_4h_v22.py` — `ARM_LABELS`, the same three literals in the same order |
| Rejected | any other string, any `bool`, any non-string — all as `RUN_INVALID_OUTCOME_EVIDENCE` |
| Coercion | none. There is no `bool(...)`, no `str(...)`, no default. A value that is not exactly one of the two labels is refused, never converted |

Both halves of the DFC v2.2 work — this corpus contract and the strategy
contract registration — declare that same literal set and enforce it the same
way, so an arm value admitted here means the same thing when it reaches the
adjudication side. Admitting an arbitrary string here while the other side
coerced it to `True` is precisely the split this clause closes.

`outcome_abs_log_return_bps = |ln(next_close / t_close)| × 10000`. This is a
measurement of bar-to-bar movement. It is **not** a PnL claim and **not** a
fill-feasibility claim; no fee, slippage, spread or queue model is present, and
none may be added to this table.

---

## A2-C5 — Authenticity and freeze procedure (NW-F6)

> **NW-F6 — corpus 진정성·동결 절차** (원문 축자 인용)
>
> **“모든 원본 object/response에 endpoint·query·retrieved_at·schema·object/ZIP SHA-256·epoch별 payload SHA를 기록한다. manifest와 canonical parquet를 동결하고, 독립 검증자가 동일 public object를 재수집해 object SHA/행수/시각경계/원시 payload hash를 대조해야 admissible이다. 자기 일관성 검사만으로 Binance 진정성을 주장하지 않는다.”**


**Enforced as.** Every manifest source entry must carry `endpoint`, `query`,
`retrieved_at`, `schema`, `object_kind`, `object_sha256`, a non-empty
`payload_sha256_by_epoch` mapping, `byte_size` and `row_count`; every canonical
table entry must carry `path`, `sha256`, `row_count` and `byte_size`; and the
manifest must declare `frozen = true`. Any missing or empty provenance field
raises `RUN_INVALID_AUTHENTICITY_EVIDENCE`.

Admissibility is a separate, explicit declaration:
`admissibility.basis` must be `independent_recollection`,
`independent_recollection_required` must be `true`, and `comparison_keys` must be
exactly `("object_sha256", "row_count", "time_bounds", "raw_payload_sha256")`.
A basis of `self_consistency` is rejected by name. Internal consistency of this
corpus with itself is not evidence that Binance produced it; only a verifier who
re-fetches the same public object and matches those four keys makes it
admissible.

---

## Terminal codes

| Code | Raised when |
|---|---|
| `RUN_INVALID_SCOPE_SEPARATION` | A1 (or anything else) declared as the DFC prerequisite (A2-C1) |
| `RUN_INVALID_CORPUS_LITERALS` | a frozen id, path, window, universe rule or imputation literal was changed (A2-C2) |
| `RUN_INVALID_FORBIDDEN_SOURCE` | funding / open interest / mark / index material present (A2-C3) |
| `RUN_INVALID_TABLE_SCHEMA` | canonical table column set, order, type or nullability violated (A2-C3) |
| `RUN_INVALID_MANIFEST_SCHEMA` | manifest key set wrong, or required source kind / canonical table absent |
| `RUN_INVALID_OUTCOME_EVIDENCE` | any outcome-semantics violation, including a deleted row (A2-C4) |
| `RUN_INVALID_AUTHENTICITY_EVIDENCE` | provenance missing, freeze evidence missing, or a self-consistency admissibility claim (A2-C5) |

No code is downgradable to a warning, and no validator repairs its input.

## Out of scope

This contract fixes *what the corpus must be*. It does not fix, and must not be
read as fixing:

- the A2 measurement itself (`READY` / `NOT_READY` / `UNDETERMINED`) — separate job;
- the DFC-2C-4H-v2.2 strategy contract, its harness or its pass rule — separate job;
- corpus collection, the historical run, or any Futures Demo execution decision.

Nothing here may be revised after A2 measurement results or backtest results are
read. Changing a literal in this file after that point is not a correction; it is
a different contract, and it needs a new ID and a new signature.

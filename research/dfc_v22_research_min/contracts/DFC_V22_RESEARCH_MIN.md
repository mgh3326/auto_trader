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
| Amendment binding record | same file, §26차 (A2-C6 · A2-C7 · A2-C8) |
| Amendment binding record | same file, §31차 (A2-C9) |
| Amendment binding record | same file, §34차 2항 (A2-C9 enumeration 38 → 49) |
| Schema | `research/dfc_v22_research_min/schema.py` |
| Validators | `research/dfc_v22_research_min/validation.py` |
| Golden cases | `tests/research/dfc_v22_research_min/golden/violation_cases.json` |

The five upstream clauses are reproduced below **verbatim**, in the language they
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

## A2-C6 / A2-C7 — Lifecycle authority, substitute evidence, archive gap (OD-26)

> **§26차 Job A** (원문 축자 인용)
>
> **Job A (②+①)**: ②contract lifecycle 권위 소스 = 「없다」를 계약에 명시, 대체 증거 정의 — eligibility = kline 아카이브 자체(랭킹 창 내 완전한 4h kline + 비zero 거래량 = 거래가능의 직접 증거), 프록시 한계 명기. ①premiumIndexKlines ~70 심볼 격차 전수 diff(read-only) → epoch별 top-3 후보 pool 과 교집합: 0 이면 `NO_IMPACT` 리터럴 종결, 비어있지 않으면 해당 epoch = `RUN_INVALID_INPUT_EVIDENCE` (조용한 재랭킹 금지) 를 계약에 추가.

A2-C2/A2-C3 list `contract lifecycle/eligibility evidence` among the required
source material. These two clauses say what that material *is*, now that
A2-MEASURE has looked for it.

### A2-C6 — There is no authoritative public lifecycle source

**There is no single authoritative public Binance source for contract lifecycle
or eligibility.** This is a measured absence, not an unfinished search:
A2-MEASURE looked, found two partial and non-identical proxies, and corroborated
one of them against exactly one historical event. `contract.py` therefore freezes
`LIFECYCLE_AUTHORITATIVE_PUBLIC_SOURCE = None`, and `validate_manifest` rejects
any corpus that names one.

The two proxies, and what each **cannot** answer:

| Proxy | Answers | Does not answer |
|---|---|---|
| `exchangeInfo.symbols[].onboardDate` | when a **currently trading** contract was onboarded | anything about delisted or settled contracts, which are absent from the endpoint entirely, so the historical universe cannot be reconstructed from it. It is also not the first tradable epoch: UNFIUSDT's first 4h kline is 10 days after its `onboardDate` |
| first/last monthly archive object per symbol | a coarse outer bound on when data exists | whether the contract was *listed and tradable*, since this is an inference from data presence rather than a record Binance publishes and stands behind. Corroborated against one real event (LUNAUSDT, 2022-05); silent about intra-month halts |

Neither proxy may be declared as the eligibility evidence kind, and neither may
be recorded as authoritative. `proxy_limits` must carry both limitation texts
unchanged, so that a later reader cannot find a softened version of them.

**Substitute evidence (the definition OD-26 signs).** Eligibility is decided by
the **kline archive itself**: a symbol is eligible at an epoch when the ranking
window holds **complete 4h klines with non-zero traded volume**. A completed bar
with volume in it is direct evidence that the contract was tradable in that bar.

This is **a different kind of evidence, not an approximation of the missing
authority.** A lifecycle record would be a statement about listing status; this
is a trace of trading having happened. They answer different questions, and
neither stands in for the other in general. What OD-26 rules is narrower: for
*ranking-window eligibility in this corpus*, the direct evidence is what is used.
Nothing here claims the two are interchangeable, and the contract is not to be
read as having recovered the authority it says does not exist.

**Enforced as.** `manifest.lifecycle_eligibility` must declare
`authoritative_public_source = null`, `evidence_kind = "kline_archive_direct"`,
and `proxy_limits` equal to the frozen texts. Naming a source, or promoting
either proxy to the evidence kind, raises `RUN_INVALID_INPUT_EVIDENCE`.

### A2-C7 — The premium-index archive gap, and the per-epoch verdict

The `klines` and `premiumIndexKlines` monthly archives do not carry the same
symbol set. Every symbol in the difference is accounted for; the corpus does not
get to treat the difference as empty.

**The verdict literals.** Per epoch, the intersection of the gap set with that
epoch's top-3 candidate pool is either empty or not:

| Intersection | Epoch verdict |
|---|---|
| empty | `NO_IMPACT` |
| non-empty | `RUN_INVALID_INPUT_EVIDENCE` |

**Silent re-ranking is forbidden, and this is the failure OD-26 names first.**
When a gap symbol belongs in an epoch's top 3, the epoch is marked invalid. It
is **not** repaired by dropping that symbol and promoting the next-ranked one:
that would convert an input-evidence failure into a clean-looking ranking, and
the resulting corpus would carry no trace that anything was wrong. There is no
substitution path in the contract and none in the code —
`validate_premium_index_gap` returns nothing, names no replacement, and hands
back no pool.

**Enforced as.**

1. `manifest.premium_index_gap` records the full diff (`symbols`), the subset
   requiring per-epoch audit (`in_window_symbols`), the listing endpoint and
   retrieval time, and the measurement's SHA-256.
2. Every symbol in `symbols - in_window_symbols` carries an exclusion with
   evidence and a reason from a **closed** set — `not_perpetual`,
   `no_kline_evidence_in_corpus_span`, `same_instrument_as_base`. An unaccounted
   gap symbol is a violation, so "it did not look relevant" cannot be silent.
   `same_instrument_as_base` must name the base symbol, and that base may not
   itself be in the gap set.
3. The `premium_index_gap_audit` table carries one row per (epoch, in-window gap
   symbol). A missing row is a violation: a gap symbol is accounted for at every
   epoch, never omitted.
4. The declared pool is checked to *be* the ranking it claims — ranks exactly
   `1..3`, no duplicate symbol, and the declared order equal to the
   `quote_volume` ordering with `canonical_symbol_ascending` ties. A pool
   quietly re-ordered around a dropped symbol fails here.
5. Each audit row's `verdict` is **recomputed** from its recorded lookback
   volume against the rank-3 cut and compared, exactly as outcome numbers are in
   A2-C4. A recorded `NO_IMPACT` that the evidence does not support fails; so
   does a gap symbol found inside the pool.

Recomputation binds the verdict to the recorded volume; the *volume itself* is
held to the same standard as every other number in this corpus — A2-C5
independent re-collection — and to nothing stronger. This clause does not claim
to detect a builder that misreports a gap symbol's volume and its evidence hash
consistently.

---

## A2-C8 — Pre-registered sample readiness protocol (OD-26 Job B)

> **§26차 Job B** (원문 축자 인용)
>
> **Job B (③)**: 측정 **전** 표본 규칙 사전등록 — 판정창 분기별 고정 seed 층화표본 epoch 12 (총 ~108), 각 표본 epoch 의 top-3 후보 심볼 kline+premiumIndex 완전성 검사. **READY = 표본 100% 무결 / 미달 = NOT_READY** (UNDETERMINED 재판정 없음 — 이지선다). 전수 검증은 FREEZE 가 fail-closed 수행 — READY 는 동결 노력 투입 결정일 뿐.

Job A closed two of A2-MEASURE's three open items (lifecycle authority, the
premium-index archive-directory gap). The third — coverage was only
spot-checked, not measured across the corpus's actual shape — is what this
clause closes, without re-litigating the full 5,494-epoch sweep that §26차
explicitly rejected as duplicating the FREEZE job at no informational gain.

**The literals, frozen before any sample is drawn:**

| Literal | Frozen value |
|---|---|
| `seed` | `26` |
| `epochs_per_quarter` | `12` |
| `quarter_definition` | UTC calendar quarter (Jan–Mar / Apr–Jun / Jul–Sep / Oct–Dec), clipped to `[JUDGMENT_START, JUDGMENT_END)` |
| `selection_algorithm` | `sha256(f"{seed}:{quarter_key}:{epoch_open_time_ms}")` ascending, take the first `epochs_per_quarter` (or all, if fewer exist in that quarter) |
| `completeness_dimensions` | `kline_4h`, `premium_index_4h` |
| verdict domain | `READY`, `NOT_READY` — closed, two members, no third |

`contract.quarter_windows()` computes the quarter boundaries purely from
`JUDGMENT_START`/`JUDGMENT_END` — a calendar fact, checkable without reference
to this corpus. `contract.sample_epoch_open_times()` is a pure function of the
seed, the quarter key and the quarter boundaries: SHA-256 has no version-
dependent PRNG state to drift, so the same three inputs reproduce the same
epoch set on any machine, in any process, indefinitely. This is what makes
"the sample was drawn before the rule was edited" checkable after the fact,
rather than merely asserted: `validate_sample_readiness` recomputes
`contract.sample_plan()` and rejects a report whose `quarters` do not match it
byte-for-byte (A2-C8, `RUN_INVALID_CORPUS_LITERALS`).

**Completeness, defined.** For a sampled epoch's top-3 candidate pool (decided
by the same universe rule as A2-C2/A2-C7 — trailing 30-calendar-day quote
volume, ties broken canonical-symbol-ascending, over the then-eligible
`usdm_perpetual` universe under the A2-C6 substitute eligibility evidence), a
row is complete when both:

1. `kline_4h`: a schema-conformant `usdm_kline_4h` bar exists for that symbol
   at the epoch's `open_time`, with a matching `close_time` and no null field.
2. `premium_index_4h`: a schema-conformant `premiumIndexKlines` bar exists for
   that symbol at the same `open_time`, carrying `premium_index_close`.

A gap symbol (A2-C7) landing inside a sampled epoch's top-3 is **not** scored
as an incomplete row and is **not** silently re-ranked around: the epoch is
recorded in `run_invalid_epochs` and disqualifies `READY` on its own, exactly
as A2-C7 already rules for the full corpus.

**The verdict rule.** `READY` iff every row in the sample reports both
dimensions complete *and* `run_invalid_epochs` is empty; otherwise `NOT_READY`,
with the failing epoch/symbol/dimension named. There is no `UNDETERMINED` exit
from this specific protocol — §26차 rules it a two-way choice, and one broken
row anywhere in the sample is enough for `NOT_READY`.

**`READY` does not mean the whole corpus is clean.** It means the stratified
sample the frozen rule drew is intact — a decision to invest in building the
frozen corpus, not a claim that every one of the ~5,494 epochs will validate.
The FREEZE job still validates the full corpus, fail-closed, independently of
what this sample found.

**Enforced as.** `validation.validate_sample_readiness` requires the report's
`sample_rule` to equal `contract.SAMPLE_RULE` exactly, its `quarters` to equal
`contract.sample_plan()` exactly, every row's `epoch_open_time` to be a member
of that plan, the declared `verdict` to be one of `READY`/`NOT_READY`, and that
declared verdict to equal the value recomputed from the row evidence and
`run_invalid_epochs` — never accepted merely because it was declared.

---

## A2-C9 — The ranking-input deficit, and the enumerated epochs (OD-31)

> **§31차** (원문 축자 인용)
>
> 유형③: 랭킹 입력 결손으로 top-3 구성이 달라졌을 것으로 전수 증명된 epoch(열거 목록 고정)도 동일하게 RUN_INVALID_INPUT_EVIDENCE, 재랭킹 금지

A2-C7 fires in one direction: a gap symbol that is **inside** an epoch's top-3
pool. A2-MEASURE's successor measurement found the opposite direction. Some
symbols' `klines` archive objects carry silent internal holes — bars missing
from the middle of an actively-trading history, contradicted by the public REST
endpoint — and a missing bar is read by the trailing-30-day ranking sum as zero
volume. A symbol whose input is short that way ranks lower than its own traded
volume warrants, and can be pushed **out** of a top 3 it belonged in.

Read literally, A2-C7 does not reach this: the affected symbol is not in the
pool, which is precisely the complaint. §31차 declined to stretch the existing
wording over it — an interpretation that wide would also cover cases nobody
measured — and wrote this clause instead. It is the same verdict for a
different mechanism, arrived at explicitly.

**The epochs are an enumeration, not a rule.** The measurement that produced
them has already been read. Re-deriving the list from the corpus at validation
time would therefore be re-running a decision procedure *after* seeing its
inputs, which is the sequencing every other clause in this document exists to
prevent. So the list is frozen as a literal, and the file it was transcribed
from is pinned by digest:

| Literal | Frozen value |
|---|---|
| `enumeration_path` | `~/work/herdr-artifacts/dfc-v22-readiness-v1/gap-impact-full/unified_flip_epochs.json` |
| `enumeration_sha256` | `1dcf41ff108d2a9b98e821c93cabb242e31317cb202ed0650f38c962bda33cc4` |
| `scan_path` | `~/work/herdr-artifacts/dfc-2c-4h-v22-corpus-v1/raw/phase3_investigation/internal_gaps.json` |
| `scan_sha256` | `f8cae492dddc58323211519d7b867a1de5efd5a06a56d4ee4aea40c0fca5050a` |
| `scan_record_count` | 105 internal gaps, 53 symbols, folded into 8 windows |
| epoch count | 49 (38 + 11) |
| span | `2022-03-02T04:00:00Z` … `2022-04-06T16:00:00Z`, two contiguous 4h runs |
| shape | ranks 1–2 unchanged (`BTCUSDT`, `ETHUSDT`) at all 49; rank 3 `GALAUSDT`→`LUNAUSDT` (38) and `LUNAUSDT`→`GMTUSDT` (11) |
| verdict | `RUN_INVALID_INPUT_EVIDENCE` |

**Two digests, because they fix different things.** `enumeration_sha256` fixes
*which epochs* are enumerated. `scan_sha256` fixes the **scope against which
"every affected epoch" was earned** — the 105 internal-gap records, folded into
8 windows, that were measured one window at a time. An enumeration is only
exhaustive relative to some scan; pinning the list while leaving the scan
unpinned lets the ground move under a completeness claim that still reads as
true. Both are compared, and either one moving is rejected by name.

The digests are not decoration. `contract.py` never opens either file — this
package cannot read files at all — so the digest is what makes a list that moved
*visible* instead of silent: a manifest built against a changed list or a changed
scan declares a different digest, and the validator refuses it.

**The enumeration was amended once, 38 → 49 (§34차 2항).** §31차 froze 38 epochs
from a single measured window. A later job folded the same 105-record scan into
all 8 of its windows and measured each — reproducing those 38 unchanged, finding
11 more caused by a second window (`2022-03-31T20:00Z`…`2022-04-03T00:00Z`, 50
symbols, `GMTUSDT`'s own 12 missing bars), and positively closing the remaining
6 single-symbol windows at 0 flips each. The amendment adds epochs to the
invalid set; it removes none and softens no verdict. The pre-registration it
protects is the *list*, and the 38 already in it were carried across untouched.

**Gap window ≠ flip span, and the table above states the latter.** The window
named for each group is the interval whose *archive bars are missing*; the
enumerated epochs are the ones whose ranking that gap changes, and they fall
**after** it — a bar missing at `t` skews the trailing-30-day sum at every epoch
`E` with `t < E ≤ t+30d`. So the 2022-02-25→03-01 gap yields flips at
2022-03-02→03-08, and the 2022-03-31→04-03 gap yields flips at
2022-04-05→04-06. Read as one span, the two look like a contradiction; they are
cause and effect, and the frozen enumeration carries the effect.

**Re-ranking is forbidden here too, and here it is the more tempting
direction.** In A2-C7 the repair would be to drop a symbol; here it would be to
*promote* one — to record the ranking complete input would have produced, which
looks more accurate than the one the archive supports. It is still a ranking no
reader can reproduce from the recorded evidence, and it would leave the corpus
carrying no trace that the input was ever short. So the corpus records the
as-archived pool, marks the epoch invalid, and stops there.

**Enforced as.**

1. `manifest.ranking_input_deficit` restates the enumeration —
   `enumeration_path`, `enumeration_sha256`, `epochs`, `verdict` and one `rows`
   entry per epoch — and every one of those is compared against the frozen
   literal rather than merely type-checked.
2. Every enumerated epoch is **present** in `pit_universe`. An absent pool is a
   deleted row, which is how a deficit disappears without a trace.
3. Every enumerated epoch's declared pool equals the as-archived ranking. If
   `would_have_been_rank3` appears in the pool, that is the silent re-ranking
   this clause forbids, and it is rejected under its own message.
4. No enumerated epoch carries a decision or an outcome row.
   `RUN_INVALID_INPUT_EVIDENCE` is terminal, so an epoch that was scored was
   processed as if its ranking input had been complete.

`validate_ranking_input_deficit` returns nothing and edits nothing, for the same
reason `validate_premium_index_gap` does: there is nowhere for a repaired pool
to come out.

**What this clause does not cover.** It covers exactly the 49 enumerated epochs.
Any other archive gap — a different symbol, a different window, a deficit whose
ranking impact was never measured — is *not* absorbed by it, and a FREEZE that
meets one is a fail-closed stop, not a case for this clause. That the list grew
once does not make it growable: an out-of-enumeration gap found during a FREEZE
is a **scan failure**, escalated upstream, not a 50th row added on the spot.

**A bounded limitation, recorded rather than closed.** The scan this clause is
pinned to finds gaps by *row absence*. A row that is present but carries
`quote_volume = 0` is therefore not a gap to it, even though it contributes zero
to the same trailing sum. 47 symbols have runs of ≥3 consecutive zero-volume 4h
bars. They were inspected by shape — nearly all run to the end of that symbol's
archived data, starting at its delisting date (`FTTUSDT`/`FTTBUSD`/`SRMUSDT`/
`RAYUSDT` at the FTX collapse, `BTCSTUSDT` for 21,668h), which is the
delisting-tail pattern the eligibility mechanism already handles and where zero
is the true traded volume, not a missing one. They were **not** individually
cross-checked against REST. That check was judged disproportionate and was not
performed, so this is a shape-based judgment, not a proof. It is non-blocking for
this freeze by explicit ruling (§34차), and it is written here rather than left
in a job report so that a later reader finds the limit attached to the clause it
limits.


## Terminal codes

Adjudicated in this order — `contract.TERMINAL_CODE_PRIORITY`, enforced by
`validate_corpus`. `RUN_INVALID_INPUT_EVIDENCE` is decided **first and
unconditionally**: a corpus built on material the contract refuses is the wrong
artifact, so any downstream code reported for it would name a symptom and hide
the cause.

| Code | Raised when |
|---|---|
| `RUN_INVALID_INPUT_EVIDENCE` | lifecycle authority claimed, proxy promoted to evidence kind, gap symbol unaccounted for, gap symbol inside or outranking the candidate pool, or a recorded epoch verdict the evidence does not support (A2-C6 / A2-C7); an enumerated ranking-input-deficit epoch deleted, re-ranked, scored, or declared against a changed enumeration (A2-C9) |
| `RUN_INVALID_SCOPE_SEPARATION` | A1 (or anything else) declared as the DFC prerequisite (A2-C1) |
| `RUN_INVALID_CORPUS_LITERALS` | a frozen id, path, window, universe rule or imputation literal was changed (A2-C2), or a sample-readiness report's rule/epochs/verdict do not reproduce the frozen protocol (A2-C8) |
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

## Amendment provenance (A2-C6 / A2-C7)

That clause has to be applied to the amendment itself, in the open: **A2-C6 and
A2-C7 were written after the A2-MEASURE verdict was read.** That is exactly the
timing the paragraph above is suspicious of, so the record is:

- **No frozen literal changed.** Corpus id, root, warmup and judgment windows,
  outcome tail, universe rule (30 days · top 3 · canonical-ascending ties),
  required and forbidden source material, imputation 0, the arm-label set and
  every A2-C1..A2-C5 terminal code are unchanged. The amendment is additive: two
  clauses, one terminal code, one canonical table (`premium_index_gap_audit`),
  and two manifest keys (`lifecycle_eligibility`, `premium_index_gap`).
- **It tightens; it cannot relax.** Every added rule can only reject corpora the
  previous text would have accepted. No gate widened, no threshold moved.
- **What was read did not move a number.** A2-MEASURE returned `UNDETERMINED`
  and named two gaps. The amendment answers "which evidence does the contract
  accept here", which the signed text left unspecified. It does not adjust any
  criterion toward a result, because there is no result to adjust toward — the
  backtest count is still zero.
- **Signed separately.** §26차 of the binding record authorises it, after the
  measurement, as its own decision.

Should a *literal* ever need to change, the paragraph above still governs: new
ID, new signature.

## Amendment provenance (A2-C9)

A2-C9 has the same suspicious timing as A2-C6/A2-C7 — the list of epochs it
freezes *is* a measurement result, and it was read before the clause was
written. The record, in the open:

- **No frozen literal changed.** The judgment window is not narrowed (that
  option was on the table as "B" and was refused: shortening the window hides
  the problem without recording it). The universe rule, the lookback, the
  tie-break, `imputation 0`, the required and forbidden source kinds, NW-F5's
  corpus literals, NW-F6's authenticity definition and the
  candidate/control ≥400 sample requirement are all untouched. The amendment is
  additive: one clause, one manifest key (`ranking_input_deficit`), one
  validator, and no new terminal code.
- **It tightens; it cannot relax.** Every corpus A2-C9 rejects would have been
  accepted before it. The 49 epochs move from scored to `RUN_INVALID`, never
  the other way.
- **The alternative that would have relaxed it was refused.** Back-filling the
  missing bars from REST ("A") would have made the corpus *look* complete while
  promoting the single worst-provenanced symbol in the span — `LUNAUSDT`, the
  same symbol carrying the delisting tail and its own archive hole — into the
  top 3. §31차 refused it, and refused the NW-F6 authenticity re-definition it
  would have required.
- **The enumeration cannot grow to fit what FREEZE finds.** It is pinned by
  digest and compared row by row. A gap outside it is a stop, not an
  extension — see the last paragraph of A2-C9.
- **Signed separately.** §31차 of the binding record authorises it as its own
  decision, with the backtest count still zero.

### The 38 → 49 amendment (§34차 2항), and why it is not the growth just forbidden

The paragraph above says the enumeration cannot grow to fit what FREEZE finds,
and then it grew. Both are true, and the distinction is the whole point of the
rule, so it is written out rather than left to inference:

- **FREEZE did not absorb it.** The 2차 FREEZE attempt hit 11 flip epochs
  outside the frozen 38, and it *stopped* — it did not add a row, did not widen
  A2-C9 to reach them, and did not freeze a corpus that quietly covered them.
  That stop is the rule working, and it is what produced the escalation.
- **A separate, later job measured them; a separate decision admitted them.**
  The 11 came back through the upstream path (§34차 2항), not through the job
  that met them. The amendment is authorised the same way A2-C9 itself was.
- **The sequencing is intact because the 38 did not move.** All 38 pre-registered
  rows are reproduced unchanged, epoch for epoch and symbol for symbol; nothing
  already frozen was re-derived, re-ranked, or dropped. What changed is that
  more epochs are now refused, which is the direction the clause is allowed to
  move in.
- **Amended exactly once.** §34차 2항 authorises this single replacement. A
  second one is a new decision with a new ID, not a continuation of this one —
  an enumeration that can be topped up whenever a measurement improves is not a
  pre-registration at all.


## Amendment provenance (A2-C8)

This amendment is the opposite timing from A2-C6/A2-C7, and the record says so:

- **Written before the sample was drawn.** A2-C8 is committed to this document
  and to `contract.py` before `DFC-A2-REMEASURE` (Job B) makes its first public
  GET call. The job's own report is required to state the pre-registration
  commit SHA and the measurement start time, in that order, so the sequencing
  is checkable after the fact rather than merely asserted.
- **No frozen literal changed.** Every A2-C1..A2-C7 literal, terminal code and
  table is unchanged. The amendment is additive: one clause, one closed
  verdict domain, three frozen sampling literals, and one validator
  (`validate_sample_readiness`) that reads a report handed to it — it collects
  nothing and repairs nothing.
- **It cannot be loosened by what it finds.** The seed, the quarter
  definition and the epochs-per-quarter count are fixed here, before
  measurement; `validate_sample_readiness` recomputes the sample and the
  verdict from the frozen rule and rejects any report that disagrees, so
  neither a wider sample nor a softer verdict rule can be substituted after
  the fact without failing this validator.

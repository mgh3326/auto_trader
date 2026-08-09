"""Verbatim upstream clause text for the ``DFC_V22_RESEARCH_MIN`` (A2) contract.

Generated from the canonical upstream answer document; do not hand-edit, do not
summarize, do not paraphrase.  Every string below is a byte-exact copy of one
table cell of the canonical document, so any downstream drift is detectable by
comparing against that document's SHA-256.

The canonical document lives outside this repository (operator inbox), so tests
in this repository can only pin the *transcript*: they assert that the contract
document and the frozen literals quote these strings unchanged.

``OD_26_JOB_A_VERBATIM`` comes from the binding record rather than the answer
document, and it is stored de-wrapped: the source bullet is hard-wrapped across
five lines, so the lines are joined with a single space and the leading list
marker is dropped.  Nothing else about it is altered.  Its file is an
append-only decisions log, so no whole-file SHA is pinned for it — the clause
text itself is the pin.
"""

from __future__ import annotations

CANONICAL_SOURCE_PATH = "~/work/herdr-inbox/answer-codexmock-next-wave-1630.md"
CANONICAL_SOURCE_SHA256 = (
    "df7aee908e50af42bb70dc48e09eee55dd30f881ced08e0145f8015269c36693"
)
CANONICAL_SOURCE_LINE_COUNT = 137
BINDING_RECORD = "~/work/herdr-inbox/operator-decisions-20260805-0830.md \u00a725\ucc28"
#: The A2 gap-closure amendment (this file's A2-C6 / A2-C7) is bound by \u00a726\ucc28 of
#: the same record, not by the answer document above.
BINDING_RECORD_AMENDMENT = (
    "~/work/herdr-inbox/operator-decisions-20260805-0830.md \u00a726\ucc28"
)

NW_F2_TOPIC = "A1과 DFC 최소 corpus의 범위 분리"
NW_F2_VERBATIM = "**“A1의 funding/OI/mark/index 종합 readiness는 보존하되 DFC-v2.x의 선행조건으로 쓰지 않는다. 데이터 접촉 전에 `DFC_V22_RESEARCH_MIN` A2 계약을 새 ID/SHA로 등록한다. A2는 kline OFI·premium-index·PIT universe·outcome evidence만 판정한다.”** 이는 결과를 본 뒤 게이트를 완화하는 것이 아니라, 아직 백테스트 0회인 상태에서 계약-데이터 불일치를 교정하는 것이다."

NW_F4_TOPIC = "outcome 의미론"
NW_F4_VERBATIM = "**“signal epoch t의 `BasketDecision.candidate_any`가 arm label, 같은 decision의 winner가 심볼이다. outcome은 그 심볼의 완결된 t kline close부터 즉시 다음 완결 4h kline close까지의 absolute log return bps다. 둘 모두 raw evidence에서만 생성한다. 다음 bar가 없거나 불완전하면 행을 임의 삭제하지 않고 `RUN_INVALID_OUTCOME_EVIDENCE`; 마지막 signal을 위해 corpus에는 한 개의 다음 4h bar를 추가한다. 이는 PnL/체결 가능성 주장이 아니다.”** 자유 bool·자유 가격 입력을 금지한다."

NW_F5_TOPIC = "corpus 동결 리터럴"
NW_F5_VERBATIM = "**“corpus ID=`dfc-2c-4h-v22-corpus-v1`, root=`/Users/mgh3326/work/herdr-artifacts/dfc-2c-4h-v22-corpus-v1/`; warmup `[2021-02-02T00:00Z,2021-05-02T00:00Z)`, 판정창 `[2021-05-02T00:00Z,2023-08-04T00:00Z)`, 마지막 outcome용 다음 4h bar 포함. 매 epoch 직전 30 calendar day quote-volume으로 당시 eligible USD-M perpetual 전수를 순위화해 top 3, 동률은 canonical symbol 오름차순. 필수 원문은 Binance USD-M 4h kline 12필드와 premiumIndex 4h close, contract lifecycle/eligibility evidence다. funding/OI/mark/index는 이 corpus에 넣지 않는다. imputation 0.”**"

NW_F6_TOPIC = "corpus 진정성·동결 절차"
NW_F6_VERBATIM = "**“모든 원본 object/response에 endpoint·query·retrieved_at·schema·object/ZIP SHA-256·epoch별 payload SHA를 기록한다. manifest와 canonical parquet를 동결하고, 독립 검증자가 동일 public object를 재수집해 object SHA/행수/시각경계/원시 payload hash를 대조해야 admissible이다. 자기 일관성 검사만으로 Binance 진정성을 주장하지 않는다.”**"

OD_26_JOB_A_TOPIC = "A2 공백 ②+① 폐쇄 경로"
OD_26_JOB_A_VERBATIM = "**Job A (②+①)**: ②contract lifecycle 권위 소스 = 「없다」를 계약에 명시, 대체 증거 정의 — eligibility = kline 아카이브 자체(랭킹 창 내 완전한 4h kline + 비zero 거래량 = 거래가능의 직접 증거), 프록시 한계 명기. ①premiumIndexKlines ~70 심볼 격차 전수 diff(read-only) → epoch별 top-3 후보 pool 과 교집합: 0 이면 `NO_IMPACT` 리터럴 종결, 비어있지 않으면 해당 epoch = `RUN_INVALID_INPUT_EVIDENCE` (조용한 재랭킹 금지) 를 계약에 추가."

#: §26차 확정 문단의 두 번째 항목, Job B(③).  Job A와 같은 이유로 이 문서의 소스가
#: 아니라 바인딩 레코드 자체가 소스이고, 하드랩된 원문 두 줄을 공백 하나로 합치고
#: 목록 마커만 제거했을 뿐 그 외에는 손대지 않았다.
OD_26_JOB_B_TOPIC = "A2 공백 ③ 폐쇄 경로(사전등록 표본 프로토콜)"
OD_26_JOB_B_VERBATIM = "**Job B (③)**: 측정 **전** 표본 규칙 사전등록 — 판정창 분기별 고정 seed 층화표본 epoch 12 (총 ~108), 각 표본 epoch 의 top-3 후보 심볼 kline+premiumIndex 완전성 검사. **READY = 표본 100% 무결 / 미달 = NOT_READY** (UNDETERMINED 재판정 없음 — 이지선다). 전수 검증은 FREEZE 가 fail-closed 수행 — READY 는 동결 노력 투입 결정일 뿐."

VERBATIM_CLAUSES: dict[str, str] = {
    "NW-F2": NW_F2_VERBATIM,
    "NW-F4": NW_F4_VERBATIM,
    "NW-F5": NW_F5_VERBATIM,
    "NW-F6": NW_F6_VERBATIM,
    "OD-26": OD_26_JOB_A_VERBATIM,
    "OD-26-JOB-B": OD_26_JOB_B_VERBATIM,
}

VERBATIM_TOPICS: dict[str, str] = {
    "NW-F2": NW_F2_TOPIC,
    "NW-F4": NW_F4_TOPIC,
    "NW-F5": NW_F5_TOPIC,
    "NW-F6": NW_F6_TOPIC,
    "OD-26": OD_26_JOB_A_TOPIC,
    "OD-26-JOB-B": OD_26_JOB_B_TOPIC,
}

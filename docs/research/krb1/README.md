# KR-B1 P0 준비물

이 디렉터리는 봉인 정본
`KRB1-CSM60-H5-v1`(`d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1`)
을 수정하지 않고 P0 시작에 필요한 파일만 고정한다.

## P0-4 journal

`p0-anchor-ledger.initial.jsonl`은 봉인 JSON의 초기 row/index/row hash/chain head를
바이트 그대로 옮긴 한 줄짜리 read-only scaffold다. P0 시작 시 `init`으로 gitignored
runtime journal을 독점 생성한다. 다음 쓰기는 기존 줄을 바꾸지 않고
`scripts/krb1_p0_journal.py append --row-file ...`로만 추가한다. 서비스는 매 append
전에 전체 prefix의 canonical JSON, row hash, chain hash를 검증하고 advisory lock,
단일 append, `fsync`를 수행한다.

`p0-retrospective-row.schema.json`은 §6의 필드를 다음처럼 일대일로 보존한다.

| 정본 표기 | JSON field |
|---|---|
| rank | `rank` |
| M60 | `M60` |
| DV20 pct | `DV20_pct` |
| planned limit | `planned_limit` |
| 실체결가 | `actual_execution_price` |
| 수량 | `quantity` |
| 예정·실제 청산일 | `planned_exit_date`, `actual_exit_date` |
| gross·base·stress net | `gross`, `base_net`, `stress_net` |
| 틱 bp | `tick_bp` |
| 수수료·세금 | `fees`, `taxes` |
| 지연 | `delay` |
| hash | `hash` |
| correlation_id | `correlation_id` |

§7.4가 DB 원장을 요구하지 않으므로 새 DB 테이블이나 migration은 만들지 않았다.

## ROB-1115 / PR #1700 판단

PR #1700의 `research.strategy_learning_events`는 UPDATE/DELETE/TRUNCATE를 DB
trigger로 거부하고 idempotent INSERT를 제공한다. 그러나 그 테이블은 학습결과
메모리(`stage`, `verdict`, `failure_class`, `learning_payload` 등)이고, KR-B1이
요구한 CSV/JSON row hash-chain 및 §6 retrospective 필드가 없다. 또한 PR은 아직
open이며 main에 없다.

따라서 PR #1700 테이블을 복제하거나 P0 journal로 오용하지 않는다. ROB-1115가
재사용한 ROB-846 hash helper도 raw row를 typed AST로 바꾸므로, 봉인 ledger의
plain-JSON row hash와 바이트가 달라 직접 재사용할 수 없다. 파일 원장은 정본의
`sort_keys=True`, `separators=(",",":")`, `ensure_ascii=False`,
`allow_nan=False`를 그대로 구현하고 봉인 초기 row hash/head 재현 테스트로 잠근다.
PR #1700이 merge된 뒤 P0 결과를 학습 메모리로 요약하는 것은 가능하지만, 그 row가
이 hash-chain journal을 대체하지 않는다.

## Calendar

`krx-calendar-2026-07-30-p0.json` 자체가 판정 권위다. XKRX 4.13.2 결과를 KRX 공식
휴장/정규장 규칙과 대조한 뒤 10개 날짜와 운영 시각을 파일에 고정했다. 이후
`exchange_calendars` 결과가 바뀌어도 동적 재계산으로 날짜를 바꾸지 않는다.

세션은 `2026-07-30`, `07-31`, `08-03`, `08-04`, `08-05`, `08-06`, `08-07`,
`08-10`, `08-11`, `08-12`다. 범위 안 제외일은 주말 `08-01`, `08-02`, `08-08`,
`08-09`이며 반차는 없다.

## 운영

P0 시작 전 read-only smoke:

```bash
uv run python scripts/krb1_p0_smoke.py \
  --sealed-json ~/work/herdr-inbox/krb1-combined-canonical-2026-07-28.json
```

journal 단독 검증:

```bash
uv run python scripts/krb1_p0_journal.py init
uv run python scripts/krb1_p0_journal.py verify
```

P0 시작 시에는 별도 JSON object 파일에 start anchor/manifest를 작성한 뒤 append한다.
봉인 문서가 지정하지 않은 start-anchor 세부 필드는 이 준비물에서 임의로 고정하지
않는다. 비용 probe는 `cost-probe-plan.md` 순서로 P0에서만 수행한다.

## 정본 모호성

- §6의 `hash`가 어느 단일 artifact hash인지 이름을 특정하지 않는다. 스키마는 키를
  `hash` 그대로 두었다.
- §6의 `tick bp` 계산 분모·반올림, `delay` 단위가 특정되지 않는다. 필드만 고정하고
  계산식을 추가하지 않았다.
- 비용 실측값을 `C_stress_cap`으로 축약하는 reducer/반올림 식이 없다.
- tick band별 표와 상·하한가 산식의 반올림 세부가 없다. P0-1/P0-2 증거로
  닫히지 않으면 fail-closed 한다.
- next append가 P0 시작 anchor/manifest라고만 되어 있고 그 payload 필드는 없다.
  따라서 start row의 세부 스키마는 만들지 않았다.

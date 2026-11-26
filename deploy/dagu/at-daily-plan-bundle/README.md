# KR 일일 계획 입력 번들 Dagu 사본 (hk 6902)

`at-daily-plan-bundle.yaml`은 평일 08:20 KST에 한 번 실행하도록 작성한 Dagu 복사본이며
**저장소가 등록하지 않는다**. 등록은 별도 운영자 승인 대상이다.

- 진입점: `scripts.build_daily_plan_bundle --date <KST 오늘> --commit`
- DB 만 읽는다(브로커·주문 호출 없음). 쓰기는 `review.daily_plan_input_bundles` 와 그날의
  `review.daily_plan_heads` 행뿐이며 계획 버전은 쓰지 않는다.
- 번들 마감은 기본 08:20 KST다. 그 전에 실행하면 `cutoff_not_reached` 로 거부한다(종료 코드 2).
- 오늘이 XKRX 거래일이 아니면 `skipped` 이고 종료 코드 0이다.
- 수동 확인: `uv run python -m scripts.build_daily_plan_bundle --date 2026-11-27` (기본 dry-run, 쓰기 없음).
- 상세 계약: `docs/agent-contracts/market-data-and-screener-contracts.md` 의
  「일일 계획 입력 번들 조립」.

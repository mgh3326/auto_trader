# KIS US 오래된 주문 추론 종료 (hk 4178)

운영자 결정 `decision/2026-11-12/at-b02-reconcile-and-3792#2`의 수동 경로다. 브로커 원본이 없는 추론이며 체결 증명이 아니다. 배포 마이그레이션과 실행은 별도 운영자 승인 후에만 한다. 스케줄러와 브로커 호출은 없다.

1. 배포 SHA와 마이그레이션 승인·적용 상태를 확인하고 명시된 DB URL 환경 변수 이름을 준비한다.
2. `uv run python -m scripts.expire_kis_us_rows_by_inference --database-url-env NAME`으로 preview한다. 각 행의 일곱 조건을 검토하고 불명이나 실패는 제외한다. `inference_checks`의 `conflict`는 추론 종료와 모순되는 in-effect 체결이며 `unknown`은 판독 불가다. 둘 다 조사하고 자동 수정하지 않는다. 비밀 값은 명령행에 넣지 않는다.
3. 적격 ID 전체가 최대 45개이고 운영자 검토가 끝났을 때만 같은 명령에 `--commit --ids ID,ID --decision-ref 'decision/2026-11-12/at-b02-reconcile-and-3792#2' --actor ACTOR --reason REASON`을 추가한다. 잠금 후 적격 집합이 달라지면 전체가 거부된다. 잠금 대기는 최대 2초, 개별 SQL은 5초, 트랜잭션 전체는 10초로 제한하며 시간 초과 시 preview와 감사 행을 다시 확인한다. 커밋 응답 중 시간 초과는 결과가 불명일 수 있으며 같은 완료 배치의 재실행은 no-op다. 같은 완료 배치 재실행은 무변경이다.
4. 반환된 건수와 `review.kis_us_inference_expiry_events`, 주문 행의 `last_reconcile_detail`을 확인한다. 오류 시 먼저 다시 preview한다.

DAY 근거는 저장된 limit/market, KIS US 송신의 ORD_DVSN 00/01, ROB-671의 `us_day_order` 범주다. 접수 거래일 종료 `20:00 America/New_York`는 보수적 상한이며 브로커 만료 계약이 아니다. 이후 90일 초과가 필요하다. 조건 (d)는 (c)를 함의하지만 둘을 따로 표시한다. 접수 시각과 대사 범위가 없거나 읽을 수 없으면 닫지 않는다. 감사 행은 수정·삭제할 수 없다.

마지막 재검사와 커밋 사이의 체결은 경합으로 들어올 수 있다. 체결 원장 writer를 잠그지 않는 수용된 위험이며, deadline+90일을 넘긴 DAY 주문의 해당 체결은 모순 증거다. #1112와 같이 체결과 추론 마커를 보존하고 이후 preview의 `inference_checks`로 확인한다. 점검은 각 감사 연결 행의 거래일 동일 주문 및 접수~상한 동일 종목의 in-effect 체결을 다시 조회한다.

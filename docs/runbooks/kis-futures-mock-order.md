# KIS 국내 선물 모의 주문 수동 스모크 (hk 5946)

미니 KOSPI200 선물 **모의(주간)** 지정가 1계약. 운영자가 머지 후 직접 한다. 계약은
`docs/agent-contracts/broker-and-ledger-contracts.md` 의 "KIS 국내 선물 모의 주문 경로".
이 문서의 명령은 실행 승인이 아니다 — 스케줄러·MCP 도구·자동 실행은 없다.

## 0. 준비

- 운영자가 KIS 선물옵션 모의 계좌(상품코드 `03`)를 갖고 있어야 한다(조사 `docs/research/5892-futures-options-survey.md` Q2).
- 환경(값은 출력하지 않는다): `KIS_FUTURES_READ_ENABLED=true`, `KIS_FUTURES_MOCK_ENABLED=true`,
  `KIS_FUTURES_MOCK_ACCOUNT_NO`(8자리), 선물옵션 계좌 전용 `KIS_FUTURES_MOCK_APP_KEY`·`KIS_FUTURES_MOCK_APP_SECRET`
  (주식 모의 `KIS_MOCK_*` 키로 대체하지 않는다 — 누락은 네트워크 전에
  `futures_mock_credentials_missing` 으로 거부된다), `DATABASE_URL`. 스모크가 끝나면 두 게이트를 끈다.
- 마이그레이션 `20261121_hk5946_futures_mock` 이 적용돼 있어야 한다(`uv run alembic current`, 적용은 운영자).
- **달력 확인**: `app/services/kis_futures_mock/session_calendar.py` 의 시드 범위(2026-11-23~2026-12-30)는
  KRX 공지로 확인되지 않았다. 스모크 날짜가 범위 안이고 개장일인지 KRX 공지와 대조한다. 범위 밖이면
  `calendar_unknown` 으로 거부되고, 고치려면 KRX 공지를 근거로 PR 로 수정한다.
- 종목: 종목마스터(`uv run python -m scripts.kis_futures_read`)의 미니 선물(`미니F YYYYMM`) 단축코드(예: `A05612`).
  최종거래일 당일에는 신규 진입이 거부된다.

## 1. 미리보기 (기본 — 쓰기·예약·전송 없음)

```bash
uv run python -m scripts.kis_futures_mock_order place --symbol A05612 --side buy --price 400.00
```

출력의 `status` 가 `preview` 이고 `position_effect`·`notional_krw` 가 의도와 맞는지 본다. `refused` 면
`error_code` 가 이유다(예: `daily_loss_unverified`, `max_open_positions_exceeded`, `calendar_unknown`).
종료 코드: 0 성공/미리보기, 2 거부, 3 `rejected`, 4 `uncertain`, 1 오류.

## 2. 진입 한 건 (`--confirm`)

```bash
uv run python -m scripts.kis_futures_mock_order place --symbol A05612 --side buy --price 400.00 \
  --strategy operator-smoke --confirm
```

- `--strategy` 는 필수다(없거나 placeholder 면 `attribution_required`/`placeholder_strategy` 로 전송 없음).
- `accepted` 는 접수일 뿐 체결이 아니다. 체결은 T4(통보)·T5(대사)가 증명한다 — 그 전에는 HTS/모의 계좌 화면으로 확인한다.
- 같은 날 같은 내용을 다시 보내려면 `--rung <구분자>` 가 필요하다. 같은 키는 `duplicate_intent` 로 거부된다.

## 3. 취소

```bash
uv run python -m scripts.kis_futures_mock_order cancel --broker-order-id <ODNO>            # 미리보기
uv run python -m scripts.kis_futures_mock_order cancel --broker-order-id <ODNO> --confirm
```

오늘 이 레인이 보낸 `accepted`/`pending`/`partial` 주문만, 원주문당 한 번 취소된다.

## 4. 청산 (가드)

청산 주문(반대 방향)은 같은 `place` 명령이다. 롱 청산 매도가 `진입가×0.98` 미만이거나 숏 청산 매수가
`진입가×1.02` 초과면 `requires_human_card` 로 거부된다(사람 카드 — 이 레인에는 카드 발급이 없으니 가격을 조정하거나 HTS 로 처리).

## 5. 불확실(uncertain)·미검증 응답

`status=uncertain` 이면 **다시 보내지 않는다**. 예약이 남아 같은 키는 거부된다. 모의 계좌 화면에서 주문 존재를 확인하고
(T5 대사 전에는 수동), 필요하면 다른 `--rung` 으로 새 주문을 낸다. 스모크에서 확인할 미검증 사항:
단축코드(`A05612`)가 주문 TR 에 받아들여지는지, 잔고의 방향·진입가 필드, 취소 TR 값, 응답 형태.

## 6. 하지 않는 것

옵션·야간·실계좌·일반 KOSPI200 선물·시장가·자동 롤오버·스케줄러 등록. 상한(1계약·명목 5,000만원·일일 손실 100만원·가드 2%)은
인자·env 로 바꿀 수 없다.

# KIS mock US 현금 조회 TR probe (ROB-951)

`scripts/kis_mock_us_cash_probe.py`는 KIS 모의 서버가 다음 **조회 전용** TR을
실제로 지원하는지 측정하는 default-disabled 진단 도구다.

- `VTTS3007R` — 해외주식 매수가능금액조회
- `VTTC0869R` — 통합증거금조회

이는 배선이나 주문 도구가 아니다. **이 스크립트는 주문·정정·취소 API를 절대
호출하지 않는다.** 두 대상은 모두 고정된 `GET` 조회 endpoint이고, 스크립트에는
주문 endpoint/TR 또는 broker mutation 경로가 없다.

이 위치를 새 런북으로 선택했다. 인접 `kis-mock-*.md` 런북들은 보유·주문 smoke의
운영 절차이고, 이 문서는 아직 확정되지 않은 두 cash TR의 독립 증거를 기록한다.

## 안전 게이트와 사전 조건

기본값은 비활성이다. 아래 전용 게이트가 정확히 `true`가 아니면 스크립트는 즉시
exit 0으로 끝나며 KIS client, 토큰, HTTP 요청을 만들지 않는다.

```bash
KIS_MOCK_US_CASH_PROBE_ENABLED=true
```

명시 실행 시에는 다음 기존 KIS mock credential이 필요하다. 누락 시 값이 아니라
**키 이름만** 출력하고 exit 3으로 끝난다.

```bash
KIS_MOCK_APP_KEY=...
KIS_MOCK_APP_SECRET=...
KIS_MOCK_ACCOUNT_NO=12345678-01
```

실행은 캡틴/운영자 지시가 있을 때만 한다. 이 PR에서는 실행하지 않는다.

## 실행

```bash
# 기본 preflight: HTTP 요청 없음
uv run python -m scripts.kis_mock_us_cash_probe

# 최우선 후보: mock 해외 매수가능금액조회
KIS_MOCK_US_CASH_PROBE_ENABLED=true uv run python -m \
  scripts.kis_mock_us_cash_probe --tr vtts3007

# mock 통합증거금조회
KIS_MOCK_US_CASH_PROBE_ENABLED=true uv run python -m \
  scripts.kis_mock_us_cash_probe --tr vttc0869

# 둘 다: 순차 실행하며 호출 사이 0.25초 간격을 둠
KIS_MOCK_US_CASH_PROBE_ENABLED=true uv run python -m \
  scripts.kis_mock_us_cash_probe --tr both
```

각 호출은 JSON evidence 한 줄로 `http_status`, 원문 `rt_cd` / `msg_cd` /
`msg1`, 파싱한 `stck_cash_ord_psbl_amt`, `usd_ord_psbl_amt`, `usd_balance`,
해외 주문가능금액 후보를 출력한다. 필드가 없으면 `null`로 남긴다. 성공으로
추정하거나 0으로 보정하지 않는다.

`raw_response_redacted`는 기존 `redact_broker_response`를 재사용하고, KIS
계좌 필드(`CANO`, `ACNT_PRDT_CD`)와 현재 설정된 계좌/앱키/앱시크릿/토큰의 echo를
추가 마스킹한다. 따라서 운영 로그에는 비밀값이나 계좌번호를 남기지 않는다.

## 판정과 exit code

| 관측 | 결론 | exit |
|---|---|---:|
| HTTP 응답 + `rt_cd="0"` | 해당 mock TR은 서버가 수락했다. cash 필드는 출력값으로 별도 확인한다. | 0 |
| HTTP 응답 + `rt_cd!="0"` 또는 KIS `msg_cd`/`msg1` 거부 | broker가 요청을 거부했다. `msg_cd`/`msg1` 원문을 ROB-951 증거로 기록한다. 이는 crash가 아닌 정상 판정 종결이다. | 2 |
| credential 누락 | 실행 전 구성 불충분; 서버 지원 여부는 미판정 | 3 |
| 연결/토큰/비JSON 등 응답 전송 실패 | 서버 지원 여부 미판정; `http_status=null`과 오류를 기록 | 1 |
| gate 비활성 또는 `--tr` 미선택 preflight | HTTP 요청 없음 | 0 |

`VTTS3007R`가 `rt_cd="0"`이더라도 KRW 자동환전 주문 경로가 가능하다는 뜻은
아니다. `VTTC0869R`가 성공하더라도 `stck_cash_ord_psbl_amt` 계열 및
`usd_ord_psbl_amt` / `usd_balance`의 실제 값이 비어 있지 않은지를 별도로 보며,
환율·capability 배선은 후속 결정이다.

## 구현 경계

`KISClient(is_mock=True)`의 `_ensure_token()`과 `_request_with_rate_limit()`을
직접 사용한다. 이는 기존 rate limit, mock credential view, token cache를 보존한다.
`AccountClient.inquire_integrated_margin(is_mock=True)`는 의도적으로 호출하지
않는다. 해당 상위 래퍼는 mock에서 전송 전 fail-close 하며, 이 probe의 목적은 바로
`VTTC0869R`의 서버 측 지원을 별도로 측정하는 것이기 때문이다.

이 도구의 결과가 나오기 전에는 `app/services/brokers/kis/account.py`의 mock
integrated-margin fail-close 게이트와 그 회귀 테스트를 변경하지 않는다.

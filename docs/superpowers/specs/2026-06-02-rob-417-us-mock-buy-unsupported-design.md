# ROB-417 — kis_mock US 매수 미지원 명확화 (fail-closed UX)

- **이슈**: ROB-417 (E라인 E2)
- **유형**: Bug fix (UX / fail-closed 명확화)
- **작성일**: 2026-06-02
- **연관**: ROB-421(오케스트레이션), ROB-420(매도 라우팅 — 완료), ROB-341(mock shadow-exposure), ROB-409/407(live order ledger 경계), capability_matrix(us_dual_paper)

## 증상 / 근본 원인

`kis_mock_place_order`로 US 주식 **매수**(dry_run=False)를 제출하면 전건 거부:
`KIS mock balance precheck unavailable for <SYM>: OPSQ0002 없는 서비스 코드 입니다; refusing to submit without verified orderable cash.`

근본: `_get_balance_for_order(equity_us, is_mock=True)`가 `inquire_overseas_margin`에서 KIS 에러 OPSQ0002로 raise → `_check_balance_and_warn`의 예외 분기가 **generic** 메시지로 처리(`order_validation.py:852-868`). 이는 fail-closed(안전)지만:
- "구조적 미지원(KIS 모의투자에 해외 orderable-cash 서비스 없음)"과 "일시적 precheck 실패"를 **구분하지 않음**.
- `mock_unsupported` 태그가 없어 호출자/운영자가 미지원을 기계적으로 식별 못함.
- `_is_kis_mock_unsupported` 마커(`미지원/unsupported/not available in mock/tttc8036r`)에 OPSQ0002("없는 서비스 코드") 미포함.

`capability_matrix`(`app/services/us_dual_paper/capability_matrix.py`)는 이미 kis_mock `account_cash_read=False` + `known_unknown_fields=[cash_usd, buying_power_usd, ...]`로 OPSQ0002 미지원을 **권위 있게 문서화**(2026-05-27 live smoke 검증). `get_available_capital(kis_mock)`도 overseas margin을 `mock_unsupported`로 태그함. 즉 buy 주문 경로만 이 패턴을 안 따름.

## 기대 동작 (스코프 = ROB-421 경계)

US mock 매수가 **구조적 미지원**임을 명확한 fail-closed UX로 표면화. US 활성화(대체 orderable 경로/시드현금 가정)는 별도 product decision이라 범위 밖. 매도 라우팅 문서화는 ROB-420(완료) 커버.

## 설계 (브레인스토밍 Approach A + dry_run 경고 유지)

### 변경 표면 (read-only, migration 0, broker/order/watch mutation 없음)

- `app/mcp_server/tooling/order_validation.py` — `_check_balance_and_warn`에 US mock buy 조기 가드 + 순수 헬퍼

### Unit 1 — capability_matrix 조기 가드

순수 헬퍼:
```python
def _kis_mock_us_orderable_unsupported() -> bool:
    """KIS 모의투자가 해외(USD) orderable-cash 서비스를 제공하지 않는지
    (OPSQ0002, 2026-05-27 smoke 검증). capability_matrix를 권위 소스로 사용 —
    미래에 US mock cash 어댑터가 생겨 account_cash_read=True가 되면 가드 자동 완화."""
    from app.services.us_dual_paper.capability_matrix import get_capability_matrix

    return get_capability_matrix().get("kis_mock", {}).get("account_cash_read") is False
```

`_check_balance_and_warn` 맨 앞(네트워크 호출 전)에 가드 추가:
```python
if (
    is_mock
    and market_type == "equity_us"
    and side == "buy"
    and _kis_mock_us_orderable_unsupported()
):
    message = (
        "US mock buy unsupported: KIS 모의투자 provides no overseas "
        "orderable-cash service (OPSQ0002), so orderable cash cannot be "
        "verified. Use alpaca_paper for US paper buys; kis_mock supports KR."
    )
    if dry_run:
        return f"Preview warning: {message}", None
    err = order_error_fn(message)
    err["mock_unsupported"] = True
    err["capability"] = "kis_mock_us_orderable_cash_unsupported"
    return None, err
```

동작:
- **dry_run=True**: `(warning, None)` 반환 → `_place_order_impl`은 balance_error=None이라 프리뷰를 그대로 반환하되 경고가 명확한 unsupported (현 동작과 일관, 정보성 보존).
- **dry_run=False**: `(None, err)` 반환 → `_place_order_impl`이 즉시 err 반환(실주문 차단). `mock_unsupported=True` + `capability` 태그로 구조적 미지원이 기계적으로 식별 가능.
- **KR mock**(`equity_kr`): `inquire_domestic_cash_balance` 정상 → 가드 미적용(equity_us only). crypto/live 무영향.
- 조기 가드가 OPSQ0002 네트워크 round-trip을 **선제 차단**(결정적, KIS 호출 낭비 없음). 기존 예외 분기(`:852-868`)는 진짜 일시 실패용으로 유지(generic, `mock_unsupported` 미태깅 — 구조적 미지원과 구분).

### 에러 dict 태깅

`order_error_fn`(=`_order_error`→`_build_order_error`)은 고정 shape를 반환하므로, 반환 dict에 `mock_unsupported`/`capability` 키를 추가(dict mutable). `_build_order_error` 시그니처 무변경(다른 도구도 동일하게 ad-hoc 태깅).

## 테스트 (TDD)

`tests/`의 order_validation / place_order 테스트:

1. `_kis_mock_us_orderable_unsupported()` — capability_matrix `account_cash_read=False` 반영(True 반환).
2. US mock buy `dry_run=False` → `success=False`, `mock_unsupported=True`, `capability="kis_mock_us_orderable_cash_unsupported"`, 메시지에 "unsupported"/"OPSQ0002". **`_get_balance_for_order` 미호출**(monkeypatch spy로 네트워크 선제차단 검증).
3. US mock buy `dry_run=True` → 에러 아님(`balance_error is None`), warning에 "US mock buy unsupported".
4. KR mock buy → 가드 미적용(기존 `_get_balance_for_order` 경로 진입, monkeypatch로 호출 확인).
5. US **live**(`is_mock=False`) buy → 가드 미적용.

`_check_balance_and_warn`는 `order_error_fn`을 받으므로 테스트에서 간단한 dict-반환 스텁 주입으로 단위 검증 가능.

## 안전 경계 / Non-goals

- fail-closed 보존 — 미지원이면 실주문 차단(success 위장 금지).
- dry_run preview가 orderable 검증 못함을 `mock_unsupported`로 명시.
- broker/order/watch/order-intent mutation 없음, migration 0, read-only.
- US mock 매수 **활성화**(대체 orderable 조회/시드현금 가정)는 별도 product decision — 범위 밖.
- 매도 라우팅(toss/samsung)은 ROB-420(완료) 소관 — 본 PR 무관.
- `_is_kis_mock_unsupported` 공유 마커 리스트는 **건드리지 않음**(modify/cancel soft-cancel 의미 변경 회피) — 조기 가드는 capability_matrix 기반이라 마커 불필요.

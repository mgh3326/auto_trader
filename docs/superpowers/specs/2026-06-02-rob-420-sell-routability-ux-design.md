# ROB-420 — kis_live/kis_mock 매도 라우팅 UX 함정 해소

- **이슈**: ROB-420 (E라인 E2, read-only/UX)
- **유형**: Bug fix (UX)
- **작성일**: 2026-06-02
- **연관**: ROB-421(오케스트레이션), ROB-357(holdings account_mode provenance), ROB-407(live order ledger — order path 인접, 충돌 회피 대상), ROB-417(mock 일반화 맥락)

## 증상 / 근본 원인

`get_holdings(account_mode=kis_live/kis_mock)`는 kis·toss·samsung 멀티브로커 보유를 계좌별 그룹으로 표시하지만, `kis_live_place_order`/`kis_mock_place_order` 매도는 **KIS 브로커 서브계좌 보유분만** 라우팅. toss/samsung 서브계좌 보유 매도 시 평평한 `"No holdings found"` 반환 → "보유는 보이는데 못 파는" UX 함정.

근본 원인 2가지:
1. **provenance 오라벨**: `portfolio_holdings._provenance_account_mode`가 upbit만 특수처리하고 manual(toss/samsung) 그룹은 `routing_mode`("kis_live")를 그대로 부여 → toss 그룹이 `account_mode="kis_live"`로 보여 매도 가능처럼 오인.
2. **불투명한 매도 실패**: `order_validation._validate_sell_side`(및 인접 sell-preview)가 KIS 서브계좌에 없으면 `"No holdings found"`만 반환 — "안 가진 것"과 "다른 브로커 서브계좌 보유라 이 채널 불가"를 구분하지 못함.

## 기대 동작 (이슈 Acceptance)

매도 가능 수량을 브로커 서브계좌 단위로 명확히 하거나(주문 도구가 라우팅 가능한 수량만 명시), 매도 실패 시 구체 사유를 반환. get_holdings에 broker별 주문가능 메타 추가.

## 설계 (Approach A — 브레인스토밍 결정)

`user_id`는 display(get_holdings) 경로엔 있으나 order(place_order) 경로엔 없다(주문은 env KIS 계좌로 라우팅). 따라서 "특정 broker명 사유"는 place_order에 user_id 스레딩 = MCP 스키마 변경 + ROB-407 order-path 충돌 위험이라 **제외**(Non-goal). 대신 display-layer 권위 메타 + 제너릭 매도-실패 메시지로 함정을 해소한다.

불변식 보존: **KIS 서브계좌(source=kis_api) 보유만 kis_live/kis_mock 매도 가능; toss/samsung/manual은 reference-only**(Toss reference는 sellable로 병합 금지).

### 변경 표면 (read-only, migration 0, broker/order/watch/order-intent mutation 없음)

- `app/mcp_server/tooling/portfolio_holdings.py` — get_holdings 계좌그룹에 `order_routable` 메타
- `app/mcp_server/tooling/order_validation.py` — 매도 실패 메시지 명확화 (2개 지점)

### Unit 1 — get_holdings `order_routable` 메타 (additive)

순수 헬퍼:
```python
def _account_order_routable(*, source: str | None) -> bool:
    """Manual (toss/samsung/수동) holdings are reference-only and not routable
    by any automated order tool; everything else (kis_api / upbit_api / paper)
    sells via its own channel."""
    return source != "manual"
```

`_get_holdings_impl`의 `grouped_accounts` 구성부에서 각 계좌그룹 dict에 `"order_routable": _account_order_routable(source=position.get("source"))` 추가.

- **account_mode는 무변경**(ROB-357 계약/top-level 해석 보존, additive-only). `order_routable`이 매도가능 권위 신호.
- toss/samsung 그룹 → `order_routable=False`로 함정이 명시적으로 드러남.
- 그룹의 첫 position source로 판정(한 그룹은 단일 broker/source라 일관). 방어적으로 그룹 생성 시점 position의 source 사용.

### Unit 2 — 매도 실패 메시지 명확화 (user_id 불요)

시장-인지 순수 헬퍼:
```python
def _no_holdings_sell_message(symbol: str, market_type: str, is_mock: bool) -> str:
    if market_type == "crypto":
        return f"No holdings found for {symbol} on Upbit"
    channel = "kis_mock" if is_mock else "kis_live"
    return (
        f"No sellable holdings for {symbol} in the KIS subaccount that "
        f"{channel} routes to. Holdings in other broker subaccounts "
        f"(e.g. toss/samsung) are reference-only and cannot be sold via this "
        f"channel — check get_holdings 'order_routable'/'account_mode'."
    )
```

두 지점이 같은 헬퍼 사용:
- `order_validation.py:557` (sell-preview `result["error"]`) — `is_mock`/`market_type`/`symbol` in scope.
- `order_validation.py:731` (`_validate_sell_side` → `order_error_fn(...)`) — `is_mock`/`market_type`/`symbol` in scope.

crypto 분기는 KIS 문구 오적용 방지(crypto 매도 실패는 Upbit 미보유가 원인).

## 테스트 (TDD)

`tests/test_mcp_holdings_account_mode_provenance.py`(또는 holdings 테스트 파일) + `tests/`의 order_validation 테스트:

1. `_account_order_routable(source="manual")` False / `"kis_api"`·`"upbit_api"` True.
2. get_holdings 출력: toss(manual) 계좌그룹 `order_routable=False`, kis 계좌그룹 `order_routable=True` (monkeypatch `_collect_portfolio_positions`로 manual+kis 혼합 주입).
3. equity(kr/us) 매도 실패 메시지가 KIS-서브계좌 사유 + `order_routable` 안내 포함.
4. crypto 매도 실패 메시지는 `"... on Upbit"`(KIS 문구 미포함).
5. ROB-357 account_mode provenance 회귀: 기존 테스트 전건 무변경(account_mode 미변경).

## 안전 경계 / Non-goals

- read-only / UX. broker/order/watch/order-intent mutation 없음, migration 0.
- `account_mode` 필드 무변경(additive only) — ROB-357 계약 및 top-level account_mode 해석 보존.
- Toss/samsung reference를 sellable로 병합하지 않음 — 오히려 `order_routable=False`로 명시 분리.
- place_order에 user_id 스레딩(특정 broker명 사유)은 ROB-407 order-path 충돌·스키마 변경이라 제외.
- 매도 가능 "수량"을 서브계좌 단위로 새로 노출하는 것(이슈 기대 1번 정밀판)은 범위 밖 — order_routable 플래그 + 명확 메시지로 함정 해소가 1차 목표.

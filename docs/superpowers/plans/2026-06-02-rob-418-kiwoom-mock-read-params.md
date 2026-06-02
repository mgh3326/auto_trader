# ROB-418 kiwoom_mock read 필수 파라미터 복구 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kiwoom mock 읽기 도구의 전건 실패(필수입력 파라미터 누락, return_code 2)를 복구한다 — `get_balance`(kt00018)에 `qry_tp`, `get_order_status`(kt00009)에 `stk_bond_tp`를 관례 기본값 상수로 채워 호출이 성립하게 한다(값 정확성은 operator live smoke 게이트).

**Architecture:** operator가 `return_code 2`로 증명한 누락 파라미터만 좁게 추가. 기본값은 `constants.py` 상수(Kiwoom enum 관례, smoke-확인 주석). `get_orderable_amount`(kt00010)는 unproven이라 무변경. US는 이미 KRX-only fail-closed → 런북 문서화만. read-only, migration 0, broker mutation 없음.

**Tech Stack:** Python 3.13, pytest(asyncio). 클라이언트 body dict 변경, 테스트는 FakeClient body 단언.

---

## File Structure

- Modify: `app/services/brokers/kiwoom/constants.py` — 파라미터 기본값 상수
- Modify: `app/services/brokers/kiwoom/domestic_account.py` — `get_balance`/`get_order_status` body
- Modify: `tests/test_kiwoom_domestic_account.py` — body 단언 갱신/추가
- Modify: `docs/runbooks/kiwoom-mock-smoke.md` — KRX-only/US 미지원 + 값 smoke-확인 명시

---

## Task 1: get_balance(kt00018)에 qry_tp 추가

**Files:**
- Modify: `app/services/brokers/kiwoom/constants.py`
- Modify: `app/services/brokers/kiwoom/domestic_account.py`
- Test: `tests/test_kiwoom_domestic_account.py`

배경: operator 실측 — `get_positions`/`get_orderable_cash`(no-symbol→get_balance) → `[1511:필수입력 파라미터=qry_tp]`. 현재 `get_balance` body=`{}`.

- [ ] **Step 1: Write the failing test**

`tests/test_kiwoom_domestic_account.py`의 `test_get_balance_uses_kt00018`(라인 42-46)를 갱신 + import 확인(`from app.services.brokers.kiwoom import constants` 이미 사용 중):

```python
@pytest.mark.asyncio
async def test_get_balance_uses_kt00018_with_qry_tp():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_balance()
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_BALANCE_API_ID
    # ROB-418 — kt00018 requires qry_tp (operator return_code 2 without it).
    assert call["body"]["qry_tp"] == constants.ACCOUNT_BALANCE_QRY_TP_DEFAULT
```

(기존 `test_get_balance_uses_kt00018`가 있으면 이 테스트로 대체.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-418 && uv run pytest tests/test_kiwoom_domestic_account.py -k "kt00018" -v`
Expected: FAIL — `AttributeError: ... ACCOUNT_BALANCE_QRY_TP_DEFAULT` 또는 `KeyError: 'qry_tp'`(body 빈 dict).

- [ ] **Step 3: Write minimal implementation**

(a) `app/services/brokers/kiwoom/constants.py` — `# Defaults` 섹션 근처(라인 50 부근)에 추가:

```python
# ROB-418 — Kiwoom REST account-read 필수 파라미터 기본값.
# Kiwoom enum 관례 기반 기본값. 정확한 값은 operator live mock smoke로 확정한다
# (이 세션 creds 없음). 전건실패(필수입력 파라미터 누락, return_code 2)를 호출
# 성립으로 회복하는 것이 1차 목표이며, 값의 scope 정확성은 smoke가 검증한다.
ACCOUNT_BALANCE_QRY_TP_DEFAULT = "1"  # kt00018 조회구분
ACCOUNT_ORDER_STK_BOND_TP_DEFAULT = "0"  # kt00009 주식채권구분(전체)
```

(b) `app/services/brokers/kiwoom/domestic_account.py` — `get_balance` body:

```python
    async def get_balance(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_BALANCE_API_ID,
            path=ACCOUNT_PATH,
            # ROB-418 — kt00018 requires qry_tp; omitting it returns return_code 2
            # (필수입력 파라미터=qry_tp). Value is convention-default, smoke-confirmed.
            body={"qry_tp": constants.ACCOUNT_BALANCE_QRY_TP_DEFAULT},
            cont_yn=cont_yn,
            next_key=next_key,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-418 && uv run pytest tests/test_kiwoom_domestic_account.py -k "kt00018" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-418
git add app/services/brokers/kiwoom/constants.py app/services/brokers/kiwoom/domestic_account.py tests/test_kiwoom_domestic_account.py
git commit -m "fix(ROB-418): kiwoom get_balance(kt00018)에 필수 qry_tp 추가

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: get_order_status(kt00009)에 stk_bond_tp 추가

**Files:**
- Modify: `app/services/brokers/kiwoom/domestic_account.py`
- Test: `tests/test_kiwoom_domestic_account.py`

배경: operator 실측 — `get_order_history`(get_order_status) → `필수입력 파라미터=stk_bond_tp`. 현재 `get_order_status` body=`{}`. 상수는 Task 1에서 추가됨.

- [ ] **Step 1: Write the failing test**

`tests/test_kiwoom_domestic_account.py`의 `test_get_order_status_uses_kt00009_and_passes_continuation`(라인 50-57)를 갱신:

```python
@pytest.mark.asyncio
async def test_get_order_status_uses_kt00009_with_stk_bond_tp_and_continuation():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_status(cont_yn="Y", next_key="page-2")
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDER_STATUS_API_ID
    # ROB-418 — kt00009 requires stk_bond_tp (operator return_code 2 without it).
    assert call["body"]["stk_bond_tp"] == constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT
    assert call["cont_yn"] == "Y"
    assert call["next_key"] == "page-2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mgh3326/work/auto_trader.rob-418 && uv run pytest tests/test_kiwoom_domestic_account.py -k "kt00009" -v`
Expected: FAIL — `KeyError: 'stk_bond_tp'`(body 빈 dict).

- [ ] **Step 3: Write minimal implementation**

`app/services/brokers/kiwoom/domestic_account.py` — `get_order_status` body:

```python
    async def get_order_status(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_ORDER_STATUS_API_ID,
            path=ACCOUNT_PATH,
            # ROB-418 — kt00009 requires stk_bond_tp; omitting it returns
            # return_code 2 (필수입력 파라미터=stk_bond_tp). Convention-default,
            # smoke-confirmed.
            body={"stk_bond_tp": constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT},
            cont_yn=cont_yn,
            next_key=next_key,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-418 && uv run pytest tests/test_kiwoom_domestic_account.py -k "kt00009" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-418
git add app/services/brokers/kiwoom/domestic_account.py tests/test_kiwoom_domestic_account.py
git commit -m "fix(ROB-418): kiwoom get_order_status(kt00009)에 필수 stk_bond_tp 추가

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: get_orderable_amount 무변경 회귀 가드 + 전체 account 테스트

**Files:**
- Test: `tests/test_kiwoom_domestic_account.py`

`get_orderable_amount`(kt00010, with-symbol)는 unproven이라 무변경. body가 `{"stk_cd": ...}` 그대로임을 가드(over-reach 회피 명시).

- [ ] **Step 1: Write the failing/guard test**

`tests/test_kiwoom_domestic_account.py`에 추가(기존 `test_get_orderable_amount_uses_kt00010` 보강 또는 신규):

```python
@pytest.mark.asyncio
async def test_get_orderable_amount_body_unchanged_no_qry_tp():
    # ROB-418 — kt00010 (with-symbol) was NOT proven to fail by the operator;
    # do not speculatively add params (avoid wrong/unexpected-param). Body stays
    # {stk_cd: ...}; its required params are smoke-TBD follow-up.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_orderable_amount(symbol="005930")
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID
    assert call["body"] == {"stk_cd": "005930"}
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-418 && uv run pytest tests/test_kiwoom_domestic_account.py -v`
Expected: PASS — account 테스트 전건 green(Task 1·2 + 가드 + 마스킹/order_detail 기존).

- [ ] **Step 3: (구현 변경 없음 — 가드 테스트만)**

- [ ] **Step 4: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-418
git add tests/test_kiwoom_domestic_account.py
git commit -m "test(ROB-418): get_orderable_amount 무변경 가드(kt00010 smoke-TBD)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 런북 문서화 + MCP 변형 회귀 + lint

**Files:**
- Modify: `docs/runbooks/kiwoom-mock-smoke.md`

- [ ] **Step 1: 런북에 KRX-only/US 미지원 + 값 smoke-확인 명시**

`docs/runbooks/kiwoom-mock-smoke.md`의 "KRX only" 항목(라인 19 부근) 아래에 추가:

```markdown
- **US 미지원 (KRX 전용).** kiwoom_mock은 KRX 국내주식 전용이며 US/해외 주문은
  지원하지 않는다(`_exchange_error`가 non-KRX를 네트워크 호출 전 거부). US는 별도
  product decision(미활성).
- **ROB-418 — account-read 필수 파라미터:** kt00018(잔고)는 `qry_tp`, kt00009(미체결/
  이력)는 `stk_bond_tp`를 요구한다(누락 시 `return_code 2` 필수입력 파라미터 오류).
  기본값(`qry_tp="1"`, `stk_bond_tp="0"`)은 Kiwoom enum 관례이며 **이 mock smoke로
  값의 scope 정확성을 확정**한다. kt00010(주문가능, with-symbol)의 필수 파라미터는
  smoke 확인 후 follow-up(추측 미추가). ROB-399와 동일 버그(이 fix로 covered).
```

- [ ] **Step 2: MCP 변형 + KRX 거부 회귀**

Run: `cd /Users/mgh3326/work/auto_trader.rob-418 && uv run pytest tests/test_kiwoom_domestic_account.py tests/test_mcp_kiwoom_order_variants.py tests/test_kiwoom_client_endpoint_guard.py -q 2>&1 | tail -8`
Expected: PASS — account read + MCP 변형(account-read 도구가 새 body로 broker 호출) + non-KRX 거부 회귀 green.

- [ ] **Step 3: Lint + format + ty**

Run: `cd /Users/mgh3326/work/auto_trader.rob-418 && uv run ruff check app/services/brokers/kiwoom/ tests/test_kiwoom_domestic_account.py && uv run ruff format --check app/services/brokers/kiwoom/ tests/test_kiwoom_domestic_account.py && uv run ty check app/services/brokers/kiwoom/ --error-on-warning 2>&1 | tail -5`
Expected: All checks passed / already formatted.

- [ ] **Step 4: Commit**

```bash
cd /Users/mgh3326/work/auto_trader.rob-418
git add docs/runbooks/kiwoom-mock-smoke.md
git commit -m "docs(ROB-418): kiwoom-mock-smoke 런북에 KRX-only/US 미지원 + 파라미터 smoke-확인 명시

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 결과

**Spec 커버리지:**
- Unit 1 (get_balance qry_tp) → Task 1 ✅
- Unit 1 (get_order_status stk_bond_tp) → Task 2 ✅
- over-reach 회피(get_orderable_amount 무변경) → Task 3 ✅
- Unit 2 (US/KRX 문서화) + 회귀/lint → Task 4 ✅

**Placeholder 스캔:** 없음 — 모든 코드 step에 실제 코드/명령 포함.

**Type 일관성:** `ACCOUNT_BALANCE_QRY_TP_DEFAULT`/`ACCOUNT_ORDER_STK_BOND_TP_DEFAULT` 상수명 constants 정의·client body·테스트 단언 일관. body 키 `qry_tp`/`stk_bond_tp`/`stk_cd` 일관.

**안전 경계 재확인:** read-only 조회 복구(broker order mutation 없음), migration 0, KRX-only 유지. 값 정확성=operator smoke 게이트. 증명된 누락만 추가(kt00010·dmst_stex_tp 추측 미추가). ROB-399 covered. US 활성화 Non-goal.

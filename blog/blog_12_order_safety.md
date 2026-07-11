# AI에게 주문 버튼을 줘도 될까: 실계좌 자동매매의 안전장치 설계

![주문 안전장치 설계](images/order_safety_thumbnail.png)

## 주문 코드에서 가장 위험한 줄은 재시도 로직이었다

[지난 11편](https://mgh3326.tistory.com/245)에서 AI 에이전트에게 `place_order` 도구를 줬습니다. `dry_run=True`가 기본값이니까, 에이전트가 명시적으로 `False`를 넣지 않는 한 실제 주문은 나가지 않습니다. 이 정도면 충분하다고 생각했습니다.

실계좌 운용을 시작하고 나서야 알았습니다. **주문 경로의 적은 에이전트가 아니었습니다. 네트워크였고, 브로커 API의 애매한 응답이었고, 무엇보다 "실패하면 다시 시도한다"는 상식을 코드에 그대로 옮겨 적은 저 자신이었습니다.**

에이전트가 이상한 주문을 내는 시나리오는 상상하기 쉽습니다. 그래서 다들 거기에 대비합니다. 그런데 실제로 저를 위협한 건 전혀 다른 곳이었습니다. 멀쩡한 주문 하나가 두 번 나가는 경로, 성공 응답을 받고 장부에 거짓말을 쓰는 경로, 미리보기와 다른 주문이 나가는 경로. 전부 에이전트가 아무 잘못을 하지 않아도 터지는 문제들입니다.

이번 편은 그 경로들을 하나씩 발견하고 막아온 기록입니다.

## 사건 1: 타임아웃 재시도가 만든 이중 주문 경로

시작은 평범한 HTTP 클라이언트 코드였습니다. auto_trader의 KIS API 전송 레이어에는 처음부터 재시도 로직이 있었습니다. 타임아웃이나 네트워크 오류가 나면 잠깐 기다렸다가 다시 요청하는, 어느 교과서에나 나오는 패턴입니다.

```python
# 전송 레이어의 재시도 (단순화)
for attempt in range(max_retries + 1):
    try:
        response = await client.request(method, url, ...)
        return response.json()
    except httpx.RequestError:
        if attempt < max_retries:
            await asyncio.sleep(backoff)
            continue  # 재시도
        raise
```

시세 조회에서는 이게 미덕입니다. 조회가 타임아웃으로 죽는 것보다 한 번 더 시도하는 게 낫습니다. 문제는 이 전송 레이어를 **주문 POST도 그대로 타고 있었다**는 겁니다.

주문 요청이 타임아웃됐다고 합시다. 이때 확실한 건 "응답을 못 받았다"는 사실뿐입니다. 요청이 브로커에 도달하기 전에 죽었을 수도 있고, **브로커가 주문을 정상 접수한 뒤 응답만 유실됐을 수도 있습니다.** 후자의 상황에서 재시도가 발동하면 같은 주문이 두 번 접수됩니다. 삼성전자 100만원 매수가 200만원 매수가 되는 겁니다.

"응답을 못 받은 것"과 "주문이 안 들어간 것"은 다릅니다. 이 둘을 구분하지 않는 재시도는 조회에서는 회복 로직이지만 주문에서는 이중 제출 폭탄입니다.

수정은 방향이 명확했습니다. **주문 POST는 정확히 한 번만 전송하고, 어떤 이유로든 절대 다시 보내지 않는다.** KIS의 주문·정정·취소 콜사이트 전부에 재시도 차단 플래그를 달았습니다 ([PR #1361](https://github.com/mgh3326/auto_trader/pull/1361)).

```python
# app/services/brokers/kis/domestic_orders.py
js = await self._parent._request_with_rate_limit(
    "POST",
    self._parent._kis_url(constants.DOMESTIC_ORDER_URL),
    ...
    # 주문은 절대 재-POST하지 않는다. retry_request_errors=False로
    # 타임아웃/네트워크 재시도를 끄고, max_retries_override=0으로
    # 레이트리밋(429) 재전송도 끈다. 속도 제한은 전송 전 대기로만 처리.
    retry_request_errors=False,
    max_retries_override=0,
)
```

조회 경로는 기존 재시도를 그대로 유지합니다. 주문만 예외입니다. 그리고 전송이 실패하면 빈 에러 대신 명시적인 메시지를 반환합니다.

```python
# 전송 실패 시 — 성공도 실패도 단정하지 않는다
f"주문 접수 여부 불확실 (전송 실패: {reason}). "
f"재전송하지 말고 {reconcile_tool} 도구로 실제 접수 여부를 확인하세요."
```

"불확실"이라는 상태를 일급으로 인정하는 게 핵심입니다. 에이전트에게도 "다시 보내"가 아니라 "조회해서 확인해"를 지시합니다.

Upbit은 접근이 달랐습니다. Upbit API는 주문 생성 시 `identifier`라는 클라이언트 멱등키 파라미터를 받는데, 같은 계정에서 같은 `identifier`가 재사용되면 브로커가 주문을 거부합니다. 그래서 재시도 차단에 더해 모든 주문에 고유 `identifier`를 부여했습니다.

```python
# app/services/brokers/upbit/orders.py
def _new_order_identifier() -> str:
    """Upbit 주문의 클라이언트 멱등키.

    같은 identifier가 재사용되면 Upbit이 주문을 거부하므로,
    중복 전송된 주문은 이중 체결 대신 실패로 끝난다.
    """
    return str(uuid.uuid4())
```

브로커가 멱등키를 지원하면 씁니다. 지원하지 않으면(KIS) 클라이언트 쪽에서 전송 자체를 한 번으로 강제합니다. 같은 문제, 브로커 API 사정에 따라 다른 두 가지 답입니다.

## 사건 2: 토큰 만료 처리에 숨어 있던 재귀 폭탄

첫 번째 사건을 수리하고 "이제 주문은 한 번만 나간다"고 안심했는데, 몇 주 뒤 해외주식 주문 코드를 다시 읽다가 등골이 서늘해졌습니다.

KIS API는 접근 토큰이 만료되면 주문을 거부하고 특정 에러 코드(`EGW00123`)를 돌려줍니다. 기존 코드는 이 에러를 받으면 토큰을 재발급받고 **자기 자신을 다시 호출해서 주문을 재전송**했습니다. 자연스러운 복구 로직처럼 보입니다. 문제는 이 재귀에 깊이 제한이 없었다는 겁니다.

토큰 재발급이 꼬여서 매번 만료 응답이 돌아오는 상황을 상상해 보면, 이 코드는 주문 POST를 무한히 반복합니다. 그리고 더 무서운 시나리오는 브로커가 **주문을 접수해 놓고 토큰 에러를 반환하는 식의 애매한 상태**입니다. 재시도 한 번 한 번이 전부 실주문 제출일 수 있는데, 그 횟수에 상한이 없었습니다.

사건 1은 전송 레이어의 재시도였고 이건 애플리케이션 레이어의 재시도였습니다. 같은 병이 다른 층에 하나 더 있었던 겁니다. 수정은 재전송 캡 상수 하나로 요약됩니다 ([PR #1453](https://github.com/mgh3326/auto_trader/pull/1453)).

```python
# app/services/brokers/kis/overseas_orders.py
# token-expiry 재전송 캡. 이 값을 넘으면 재-POST 대신 fail-closed(RuntimeError).
# "정확히 1회 재전송"이 응답이 아니라 코드로 강제되도록
# 세 mutation 경로(order/cancel/modify)가 공유한다.
_MAX_TOKEN_REFRESH_RESUBMITS = 1

# 주문 메서드 내부
if _is_token_expiry(js):
    if _token_retry_depth >= _MAX_TOKEN_REFRESH_RESUBMITS:
        # 캡 초과 → 재-POST 대신 fail-closed. 실 자금 다중 제출 차단.
        raise RuntimeError(error_msg)
    await self._parent._token_manager.clear_token()
    await self._parent._ensure_token()
    return await self.order_overseas_stock(
        ..., _token_retry_depth=_token_retry_depth + 1
    )
```

토큰 만료로 인한 재전송은 딱 한 번. 그래도 실패하면 에러를 던지고 멈춥니다. "한 번 더 하면 될 것 같은데"를 코드가 허용하지 않습니다. 여기서 처음으로 fail-closed라는 단어를 설계 원칙으로 의식하기 시작했습니다. **확신이 없을 때 시스템이 취할 수 있는 가장 안전한 행동은, 좋은 쪽으로 가정하고 진행하는 게 아니라 멈추는 것입니다.**

## 접수는 체결이 아니다: 장부가 거짓말을 하기 시작했다

이중 제출 경로를 막고 나니 다음 문제가 드러났습니다. 이번엔 주문이 아니라 **기록**이 문제였습니다.

초기 구현은 주문 전송이 성공하면 그 자리에서 체결 내역과 매매 일지, 실현 손익까지 기록했습니다. 지정가 주문을 넣으면 DB에는 이미 "체결됨"이라고 적혀 있는 겁니다. 그런데 지정가 주문은 체결이 안 될 수 있습니다. 하루 종일 걸려 있다가 장 마감과 함께 소멸하기도 합니다. 브로커 계좌에는 아무 일도 없었는데 제 장부에는 매수 기록과 손익이 남아 있는, 장부가 현실보다 앞서 나가는 상태가 됩니다.

원인은 개념 하나를 뭉갠 데 있었습니다. 주문 전송의 성공 응답은 <b>"접수(accepted)"</b>이지 "체결(filled)"이 아닙니다. 접수는 "브로커가 주문서를 받았다"는 뜻일 뿐, 거래가 일어났다는 증거가 아닙니다.

그래서 라이브 주문 기록을 두 단계로 쪼갰습니다 ([PR #1066](https://github.com/mgh3326/auto_trader/pull/1066), [PR #1074](https://github.com/mgh3326/auto_trader/pull/1074)).

**전송 시점에는 accepted/rejected만 기록합니다.** 체결도, 일지도, 손익도 쓰지 않습니다.

```python
# app/mcp_server/tooling/kis_live_ledger.py
def _derive_live_send_status(*, rt_cd, order_no) -> str:
    """브로커 응답에서 accepted|rejected|unknown 파생.

    성공을 지어내지 않는다: rt_cd가 0이 아니면 그건 거절의 브로커 증거다.
    """
    if rt_cd == "0":
        return "accepted"
    if rt_cd and rt_cd != "0":
        return "rejected"
    return "accepted" if order_no else "unknown"
```

**체결은 별도의 reconcile 도구가 확정합니다.** 브로커의 일별 주문 조회 API에서 주문번호로 키잉된 체결 증거를 가져와, 그 증거가 있을 때만 체결·일지·손익을 기록합니다. 증거가 없으면 주문은 계속 "접수됨, 체결 대기" 상태로 남습니다.

```
전송 시점:   place_order → 레저에 accepted 기록 (여기서 끝)
확정 시점:   reconcile 도구 → 주문번호로 브로커 체결 증거 조회
             → 증거 있음: filled 기록 + 일지/손익 반영
             → 증거 없음: accepted 유지 (아무것도 지어내지 않음)
```

예외가 하나 있는데, 암호화폐 시장가 주문은 사실상 즉시 체결되므로 전송 직후 인라인으로 reconcile을 한 번 돌려 체결을 확정합니다. 이 경우에도 "즉시 체결됐을 것"이라는 가정이 아니라 브로커 조회 결과라는 증거로 기록한다는 원칙은 같습니다.

이 구조로 바꾸고 나서 장부에 대한 신뢰가 달라졌습니다. DB에 filled라고 적혀 있으면 그건 브로커 증거를 확인했다는 뜻입니다. 성공 응답을 받았다는 뜻이 아니라요.

![주문 하나가 통과하는 게이트들 — 미리보기, 승인 해시, 멱등키 선점, 단일 전송, 접수 기록, 증거 기반 체결 확정](images/order_safety_gates2.png)
*주문 하나가 실계좌에 닿기까지 통과하는 게이트들. 각 게이트는 서로 다른 실패 경로 하나씩을 막는다*

## 미리보기와 다른 주문이 나가는 걸 막기: 승인 해시

에이전트 운용에서 주문 흐름은 보통 두 단계입니다. 먼저 `dry_run=True`로 미리보기를 하고, 그 결과를 제가(또는 상위 판단이) 확인한 뒤 실제 주문을 넣습니다. 그런데 이 두 호출 사이에 아무런 결속이 없다는 걸 깨달았습니다.

미리보기에서 "71,500원 10주 매수"를 확인했는데, 실제 주문 호출에서 파라미터가 달라져 있다면? 에이전트의 사소한 착오일 수도 있고, 두 호출 사이에 가격 정규화 로직이 다르게 동작했을 수도 있습니다. 어느 쪽이든 **제가 승인한 주문과 실제로 나가는 주문이 다른 것**입니다.

그래서 미리보기와 실주문을 암호학적으로 묶었습니다 ([PR #1364](https://github.com/mgh3326/auto_trader/pull/1364), [PR #1366](https://github.com/mgh3326/auto_trader/pull/1366)).

미리보기는 주문 내용을 정규화(호가 단위 스냅 등)한 뒤, 그 정규화된 내용에서 파생한 <b>승인 토큰(approval_hash)</b>을 응답에 담아 돌려줍니다. 유효기간은 5분입니다.

```python
# app/mcp_server/tooling/toss_approval.py (단순화)
APPROVAL_TTL_SECONDS = 300

def build_canonical_payload(*, market, symbol, side, order_type,
                            time_in_force, quantity, price, order_amount):
    """미리보기와 실주문이 공유하는 정규 주문 내용.

    quantity/price는 호가 스냅이 끝난 wire 값이어야
    양쪽에서 동일한 digest가 나온다."""
    return {
        "market": market, "symbol": symbol,
        "side": side.upper(), "orderType": order_type.upper(),
        "timeInForce": time_in_force,
        "quantity": quantity, "price": price, "orderAmount": order_amount,
    }

def encode_approval_token(canonical, *, now):
    payload = json.dumps({"iat": int(now.timestamp()), "canon": canonical},
                         sort_keys=True, separators=(",", ":")).encode()
    return f"{TOKEN_VERSION}.{base64.urlsafe_b64encode(payload).decode()}"
```

실주문 호출은 이 토큰을 받아서, **자기 자신의 파라미터로 정규 내용을 다시 계산한 뒤** 토큰 속 내용과 대조합니다. 하나라도 다르거나 5분이 지났으면 주문은 거부되고, 뭐가 달랐는지 diff와 함께 "다시 미리보기하라"는 에러가 반환됩니다.

```
preview(dry_run=True)  → 정규화 → approval_hash 발급 (TTL 5분)
place(approval_hash=…) → 같은 정규화 재계산 → 대조
                         일치: 전송 / 불일치·만료: fail-closed + diff
```

서버에 상태를 저장하지 않는 자기완결 토큰이라 DB 없이 검증됩니다. "내가 본 주문"과 "나가는 주문"이 같은 바이트라는 걸 코드가 보증하는 구조입니다.

같은 작업에서 `clientOrderId`도 손봤습니다. 원래는 주문마다 새 uuid4를 만들었는데, 이러면 같은 주문이 두 번 요청됐을 때 브로커가 구분할 방법이 없습니다. 이걸 **정규 주문 내용 + 거래일 salt에서 파생한 결정적 멱등키**로 바꿨습니다.

```python
def derive_client_order_id(canonical, *, market, now, rung=None):
    salt = trading_day_salt(market, now)   # 거래일 날짜 (KR=KST, US=ET)
    disc = "" if rung is None else str(rung)
    blob = f"{canonical_json(canonical)}|{salt}|{disc}".encode()
    return f"{PREFIX}-{hashlib.sha256(blob).hexdigest()[:16]}"
```

같은 거래일에 같은 내용의 주문이 두 번 요청되면 같은 키가 나와 브로커 단에서 중복이 걸러집니다. 날이 바뀌면 salt가 바뀌어 새 주문으로 취급되고, 같은 날 정말로 같은 주문을 한 번 더 내야 하는 경우(래더 매수 등)는 `rung` 구분자로 분리합니다.

멱등키를 받아주지 않는 KIS 쪽에는 로컬 방어를 넣었습니다. 실서버로 전송하기 직전에 멱등키를 전용 테이블에 <b>선점(reserve)</b>하고, 같은 키가 이미 있으면 전송 없이 실패시킵니다.

```python
# app/mcp_server/tooling/order_execution.py
# KIS는 브로커 멱등키가 없다 — 전송 전에 로컬 intent 행을 선점한다.
# 같은 거래일에 같은 키로 다시 전송하면 fail-closed.
try:
    await OrderSendIntentService(intent_db).reserve(
        account_scope="kis_live",
        idempotency_key=idempotency_key,
        symbol=normalized_symbol,
        side=side,
    )
except DuplicateOrderIntent:
    return order_error_fn(...)  # 전송 자체를 차단
```

DB unique 제약이 최후의 방어선이라, 프로세스가 두 개 떠 있어도 같은 주문이 두 번 나가지 못합니다.

## 나머지 가드들: 이중 게이트, 손실매도 차단, 한도

여기까지가 굵직한 사건들이고, 그 사이사이 작은 가드들도 쌓였습니다.

**이중 게이트.** `dry_run=True` 기본값만으로는 부족하다는 걸 배웠습니다. 에이전트가 흐름상 자연스럽게 `dry_run=False`를 넣는 경우가 있어서, 실주문에는 `confirm=True`를 추가로 요구합니다. 두 파라미터의 의미가 다릅니다. `dry_run=False`는 "시뮬레이션이 아니다"이고, `confirm=True`는 "실제 돈이 나간다는 것을 인지하고 있다"입니다.

```python
# 주문 mutation 도구 공통
if not dry_run and not confirm:
    return {"error": "confirm=True is required when dry_run=False"}
```

**손실매도 가드.** 어느 날 미리보기에서 시장가 매도가 가격 가드를 통째로 건너뛴다는 걸 발견했습니다. 지정가 매도에는 "평단가의 1.01배 미만이면 차단"하는 플로어가 있었는데, 시장가는 가격 파라미터가 없으니 그 검사를 그냥 지나쳤던 겁니다. 시장가 매도는 대략 현재가에 체결되므로, 같은 플로어를 현재가에 적용하는 가드를 넣었습니다 ([PR #1258](https://github.com/mgh3326/auto_trader/pull/1258)).

```python
# app/mcp_server/tooling/order_validation.py
def evaluate_market_sell_loss_guard(*, current_price, avg_price, ...):
    """라이브 시장가 매도가 실수로 손실을 확정하지 못하게 한다."""
    min_sell_price = avg_price * 1.01
    if current_price < min_sell_price:
        return (
            f"Live market sell blocked: current price {current_price} below "
            f"minimum (avg_buy_price * 1.01 = {min_sell_price}). "
            "Loss-selling is disabled on live accounts."
        )
    return None
```

"손절도 못 하는 시스템이냐"고 물을 수 있는데, 의도적입니다. 손실 확정은 실수로 일어나면 안 되는 행동이라 기본은 차단이고, 진짜 손절이 필요할 때는 별도의 sanctioned 경로를 탑니다. 그 경로는 손절 사유와 근거 기록을 먼저 요구하고, 승인 해시를 모드 설정과 무관하게 **필수**로 검증하며, 그래도 현재가 대비 슬리피지 밴드를 벗어난 헐값 매도는 막습니다. 모의계좌는 반대로 이 가드를 풀어뒀습니다. 연습 계좌에서 손절 연습을 못 하면 그게 더 이상하니까요.

**금액·건수 한도.** 11편에서 소개한 1회 주문 금액 상한과 일일 주문 건수 제한(Redis 카운터)은 그대로 유지 중입니다. 위의 모든 가드가 뚫려도 하루에 잃을 수 있는 금액의 상한을 정해두는, 가장 원시적이지만 가장 확실한 장치입니다.

정리하면 실주문 하나가 나가기까지 이런 게이트들을 통과합니다.

| 게이트 | 막는 것 |
|------|------|
| dry_run + confirm 이중 게이트 | 에이전트의 의도치 않은 실주문 |
| 승인 해시 (TTL 5분) | 미리보기와 다른 주문 전송 |
| 멱등키 선점 / 브로커 identifier | 로컬·원격 이중 전송 |
| 단일 전송 (재시도 0회) | 타임아웃발 이중 제출 |
| 토큰 재전송 캡 (1회) | 재귀 재전송 폭주 |
| 손실매도 가드 | 실수로 손실 확정 |
| 금액·건수 한도 | 위 전부가 뚫렸을 때의 피해 상한 |
| accepted-only + 증거 reconcile | 장부의 거짓 기록 |

## 마치며: 세 가지 원칙

이 안전장치들은 한 번에 설계된 게 아닙니다. 사건이 하나 터지거나 코드를 읽다 서늘해질 때마다 하나씩 쌓였습니다. 돌아보면 전부 같은 원칙의 변주였습니다.

**확실한 증거가 없으면 좋은 쪽으로 가정하지 않습니다.** 타임아웃은 "아마 안 들어갔을 것"이 아니고, 성공 응답은 "아마 체결됐을 것"이 아닙니다. 애매하면 진행이 아니라 정지가 기본값입니다(fail-closed). 재전송 캡도, 승인 해시 불일치도, 손실매도 가드도 전부 "멈추고 사람에게 보고"로 수렴합니다.

**성공 응답이 아니라 증거로 장부를 씁니다.** 접수와 체결을 구분하고, 체결은 브로커에서 주문번호로 조회한 증거가 있을 때만 기록합니다. 장부가 현실을 앞서가기 시작하면 그 위에 쌓이는 모든 판단이 오염됩니다.

**멱등성은 교과서가 아니라 실계좌로 배웠습니다.** "분산 시스템에서 exactly-once는 어렵다"는 문장은 수없이 읽었지만, 그게 내 계좌의 이중 매수를 뜻한다는 걸 체감하고 나서야 전송 레이어의 재시도 한 줄 한 줄이 다르게 보였습니다. 모의투자에서는 이 문제들이 전부 "어차피 가짜 돈"으로 덮입니다. 실계좌가 최고의 코드 리뷰어였습니다.

11편 말미에 "돈이 걸리면 설계가 어떻게 달라지는지"를 다루겠다고 했는데, 답은 이렇습니다. 기능을 추가하는 시간보다 **일어나면 안 되는 일의 목록**을 늘리고 그걸 코드로 강제하는 시간이 길어집니다. 그리고 그 목록의 대부분은 상상이 아니라 사건에서 나옵니다.

다음 편은 토스증권 Open API 연동기입니다. OAuth 토큰이 계정당 하나뿐이라 생기는 문제, 반대방향 대기주문이 있으면 주문이 거부되는 브로커 제약 같은, 새 브로커 하나를 붙일 때마다 반복되는 지뢰밭 이야기를 다룹니다.

---

**참고 자료:**
- [전체 프로젝트 코드 (GitHub)](https://github.com/mgh3326/auto_trader)
- [PR #1361: 주문 POST 타임아웃 재시도 제거 + Upbit identifier](https://github.com/mgh3326/auto_trader/pull/1361)
- [PR #1453: 토큰만료 재전송 재귀 깊이 가드](https://github.com/mgh3326/auto_trader/pull/1453)
- [PR #1066: KIS 라이브 주문 accepted-only + 증거 기반 reconcile](https://github.com/mgh3326/auto_trader/pull/1066)
- [PR #1074: US/암호화폐 라이브 주문 증거 게이트 확장](https://github.com/mgh3326/auto_trader/pull/1074)
- [PR #1364: preview→place 승인 해시 바인딩 + 결정적 멱등키](https://github.com/mgh3326/auto_trader/pull/1364)
- [PR #1366: 공유 주문 경로 승인 해시 + 전송 전 멱등키 선점](https://github.com/mgh3326/auto_trader/pull/1366)
- [PR #1258: 라이브 손실매도 하드 가드](https://github.com/mgh3326/auto_trader/pull/1258)

---

> 이 글은 AI 기반 자동매매 시스템 시리즈의 **12편**입니다.
>
> - [1편: 한투 API로 실시간 주식 데이터 수집하기](https://mgh3326.tistory.com/227)
> - [2편: yfinance로 애플·테슬라 분석하기](https://mgh3326.tistory.com/228)
> - [3편: Upbit으로 비트코인 24시간 분석하기](https://mgh3326.tistory.com/229)
> - [4편: AI 분석 결과 DB에 저장하기](https://mgh3326.tistory.com/230)
> - [5편: Upbit 웹 트레이딩 대시보드 구축하기](https://mgh3326.tistory.com/232)
> - [6편: 실전 운영을 위한 모니터링 시스템 구축](https://mgh3326.tistory.com/233)
> - [7편: 라즈베리파이 홈서버에 자동 HTTPS로 안전하게 배포하기](https://mgh3326.tistory.com/234)
> - [8편: JWT 인증 시스템으로 안전한 웹 애플리케이션 구축하기](https://mgh3326.tistory.com/235)
> - [9편: KIS 국내/해외 주식 자동 매매 시스템 구축하기](https://mgh3326.tistory.com/237)
> - [10편: 다중 브로커 통합 포트폴리오 시스템 구축하기](https://mgh3326.tistory.com/238)
> - [11편: MCP 서버로 AI 트레이딩 도구 만들기](https://mgh3326.tistory.com/245)
> - **12편: AI에게 주문 버튼을 줘도 될까 — 실계좌 자동매매의 안전장치 설계** ← 현재 글

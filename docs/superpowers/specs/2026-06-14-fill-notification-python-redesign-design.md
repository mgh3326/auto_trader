# 체결(Fill) 알림 Python 재설계 — 설계 스펙 (ROB-558)

- **날짜**: 2026-06-14
- **Linear**: ROB-558 (High)
- **브랜치/워트리**: `rob-558` @ `/Users/mgh3326/work/auto_trader.rob-558` (origin/main 1d130dac 기준)
- **범위**: Phase 1 — 체결 알림을 Python `TradeNotifier`로 렌더링 이전 + 메시지 개선 + 죽은 링크 수정 + 체결→n8n 경로 제거. (전체 n8n 디커미션은 Phase 2 별도 이슈)

---

## 1. 문제 정의

### 1-1. 체결 알림이 마지막 n8n 잔재
현재 체결(fill) 알림 흐름:

```
websocket_monitor.py (launchd 상주, KIS/Upbit WS)
  → _on_kis_execution / _on_upbit_order (state="trade")
  → normalize_kis_fill / normalize_upbit_fill → FillOrder
  → _record_execution_ledger_fill (실행원장 upsert, 중복이면 알림 skip)
  → _send_fill_notification
      → OpenClawClient.send_fill_notification()        app/services/openclaw_client.py:253
          ├─ N8N_FILL_WEBHOOK_URL 비어있으면 skip
          ├─ filled_amount < 50_000 이면 skip
          ├─ Discord: JSON payload(_build_n8n_fill_payload) → n8n webhook (n8n이 Discord 렌더)
          └─ Telegram: format_fill_message() 평문 → TradeNotifier (skip_discord=True)
```

n8n은 더 이상 운영하지 않는다(현재 Prefect 사용). 운영 환경에서 `N8N_FILL_WEBHOOK_URL`은 비어 있거나 무효이므로 **Discord 체결 알림은 발화하지 않고 Telegram만** 나간다.

### 1-2. "상세" 링크가 죽은 링크 (410 Gone)
`format_fill_message()`와 n8n payload 모두 `build_position_detail_url()`을 사용:

```
app/core/portfolio_links.py:41
  → {public_base_url}/portfolio/positions/{market}/{symbol}
```

그런데 `/portfolio`는 `app/routers/deprecated_pages.py`의 `LEGACY_PREFIXES`에 포함되어 **410 Gone**(deprecated_at 2026-02-20)으로 응답하고 `/invest/`로 안내한다. 즉 알림의 "상세"를 눌러도 죽은 페이지에 도달한다.

### 1-3. 메시지 품질 격차
- Discord(n8n payload)는 `display_name`(한글명)을 담지만, Telegram(`format_fill_message`)은 원시 심볼(`005930`, `KRW-BTC`)만 표시 → **채널 간 불일치**.
- 매도 시 실현손익, 매수 후 포지션 요약 없음.
- 최소금액 필터 `filled_amount < 50_000`이 **통화 무시** → USD 체결은 거의 전부 스킵($50,000 미만).

---

## 2. 결정 사항 (브레인스토밍 합의)

| 항목 | 결정 |
|---|---|
| 렌더링 위치 | **Python `TradeNotifier`** (Prefect 아님). 체결은 auto_trader 상주 프로세스에서 발생하고 그 프로세스가 이미 `TradeNotifier`+webhook 보유 |
| 채널 | 매수/매도 "주문 접수"와 **동일 시장 채널** (`DISCORD_WEBHOOK_KR/US/CRYPTO`) |
| 랜딩 페이지 | 기존 `/invest/stocks/{market}/{symbol}` (신규 페이지 없음) |
| 메시지 개선 | ① 한글명 양 채널 통일 ② 매도 실현손익(근사치 `~추정`) ③ 매수 포지션 요약 ④ 슬리피지 강조 |
| 실현손익 정밀도 | (체결가−평단)×수량 **근사치**, 즉시·best-effort, 정밀 FIFO는 링크 페이지가 권위 |
| 보강 실패 시 | 해당 행만 생략, **알림은 항상 발송** (fail-open) |
| 부분체결 | 현행 유지 — 매 체결마다 발송, `부분체결` 라벨 |
| 최소금액 필터 | **통화 인식화** (KRW vs USD 임계 분리) |
| n8n 제거 범위 | Phase 1은 **체결 경로만** (`send_fill_notification` + `_build_n8n_fill_payload` + 해당 테스트). 나머지 n8n은 Phase 2 |

---

## 3. 목표 아키텍처

```
websocket_monitor.py
  → 체결 감지 → FillOrder (기존)
  → _record_execution_ledger_fill (기존, 중복 skip 유지)
  → [NEW] _build_fill_enrichment(order)   ← best-effort, fail-open
  → [NEW] TradeNotifier.notify_fill(order, enrichment=...)
        ├─ Discord: _get_webhook_for_market_type(order.market_type) 임베드
        └─ Telegram: 미러 텍스트
  ✂️ OpenClawClient.send_fill_notification 호출 제거
```

### 3-1. 모듈 경계 (단위와 책임)

| 단위 | 책임 | 입력 → 출력 | 비고 |
|---|---|---|---|
| `fill_notification.py` | 정규화 (유지) + display name | raw WS dict → `FillOrder` | `normalize_*_fill`, `coerce_fill_order`, `_resolve_fill_display_name` 유지·재사용. `format_fill_message`(n8n/Telegram-via-openclaw용 평문) 및 `build_position_detail_url` 의존부는 제거/대체 |
| `formatters_discord.py` | 체결 임베드 빌더 | `FillOrder`+`enrichment` → `DiscordEmbed` | `format_fill_notification()` 신규. 기존 buy/sell 포맷터 스타일 따름 |
| `formatters_telegram.py` | 체결 텍스트 빌더 | `FillOrder`+`enrichment` → `str` | `format_fill_notification_telegram()` 신규 |
| `notifier.py` (`TradeNotifier`) | 디스패치 | `notify_fill(order, enrichment)` → bool | 시장별 webhook 라우팅, Telegram 미러. 기존 `_dispatch` 재사용 |
| `app/core/invest_links.py` (신규 or portfolio_links 교체) | 살아있는 링크 빌더 | symbol+market → `/invest/stocks/{market}/{symbol}` URL | 아래 3-4 참고 |
| `app/services/fill_enrichment.py` (신규) | 평단·수량·실현손익 조회 | `FillOrder` → `FillEnrichment | None` | fail-open. KR/US=`merged_portfolio_service.get_reference_prices`, crypto=Upbit balance avg |
| `websocket_monitor.py` | 배선 | 체결 → enrichment → `notify_fill` | n8n 호출 제거 |

### 3-2. `FillEnrichment` 데이터 모델 (신규, dataclass)

```python
@dataclass
class FillEnrichment:
    # 매수/매도 공통: 체결 후 포지션 (best-effort)
    position_qty: float | None = None          # 총 보유 수량
    position_avg_price: float | None = None     # 평단
    # 매도 전용: 실현손익 근사치 (best-effort)
    realized_pnl_amount: float | None = None    # (체결가 − 평단) × 수량
    realized_pnl_rate: float | None = None      # %
    is_approximate: bool = True                  # 항상 근사치 → ~추정 라벨
```

조회 출처:
- **KR/US**: `MergedPortfolioService.get_reference_prices(user_id, ticker, market_type, kis_holdings=None, kis_client=None)` → `ReferencePrices(combined_avg, total_quantity, kis_avg, ...)`.
  - ⚠️ 시그니처 주의: `user_id` 필요(websocket 리스너는 단일 operator 계정 → 고정 user_id), `kis_holdings` 미전달 시 내부에서 KIS API 호출(지연·실패 가능). 구현 플랜에서 (a) operator user_id 확보 방법 (b) kis_holdings 스냅샷 재사용으로 추가 API 호출 회피 여부를 확정.
  - 매도 실현손익 = `(filled_price − combined_avg) × filled_qty`, rate = `(filled_price/combined_avg − 1)×100`.
  - 매수 포지션 = `total_quantity`, `combined_avg`.
- **crypto**: Upbit balance의 `avg_buy_price`·보유수량 (정확한 호출 지점은 구현 플랜에서 확정).
- enrichment가 무거우면(추가 API 호출) 대안: 실행원장/`review.trades` 기반 경량 평단, 또는 enrichment를 옵션으로 두고 1차 릴리스는 슬리피지+한글명+링크만. 구현 플랜에서 비용 측정 후 결정.
- 모든 조회는 try/except로 감싸 실패 시 `None` 반환 → 해당 행만 생략. **알림 자체는 막지 않는다.**

> 주의: `get_reference_prices`의 평단은 "체결 반영 전" 스냅샷일 수 있다. 매수 포지션 요약은 "체결 직후 추정"이므로 라벨을 `~추정`으로 두고, 정확값은 링크 페이지가 권위. 구현 플랜에서 평단 스냅샷의 체결 반영 여부를 확인하고 라벨 문구를 맞춘다.

### 3-3. 메시지 레이아웃

**Discord 임베드** (매수=초록 `COLORS["buy"]`, 매도=빨강 `COLORS["sell"]`):

```
제목:  🟢 체결 · 삼성전자 (005930)        ← embed.url = /invest/stocks/kr/005930 (클릭)
설명:  매수 체결                            (부분체결이면 "매수 부분체결")
필드:
  체결가   68,500원 (+0.30% vs 주문가)   [inline]   ← 슬리피지 (order_price 있을 때만)
  수량     10주                          [inline]
  금액     685,000원                     [inline]
  보유     30주 · 평단 68,100원  ~추정    [inline]   ← 매수: 포지션 (enrichment 있을 때만)
  계좌     kis · 주문 0001234…           [inline]
footer: 🕒 2026-06-14 09:31:02 KST
```

매도일 때 `보유` 필드 대신:
```
  실현손익  +12,000원 (+1.8%)  ~추정     [inline]   ← (enrichment 있을 때만)
```

**Telegram 미러** (동일 정보, 마크다운 링크):
```
🟢 *체결 · 삼성전자 (005930)*
매수 체결
체결가: 68,500원 (+0.30%)
수량: 10주 · 금액: 685,000원
보유: 30주 · 평단 68,100원 (~추정)
계좌: kis · 주문 0001234…
🕒 2026-06-14 09:31:02 KST
[종목 상세 보기](https://…/invest/stocks/kr/005930)
```

- 한글명은 `_resolve_fill_display_name(order)`로 양 채널 통일. 해석 실패 시 심볼만 표시(graceful).
- 슬리피지: `order_price`가 있고 0이 아닐 때만 `(±x.xx% vs 주문가)`. 없으면 생략.

### 3-4. 링크 빌더 교체 (죽은 → 살아있는)

`build_position_detail_url()` (→ `/portfolio/positions/...`, 410)을 **`/invest/stocks/{market}/{symbol}`** 빌더로 교체한다.

- 신규 `build_stock_detail_url(symbol, market_type)`:
  - market 정규화는 기존 `normalize_position_market_type`(kr/us/crypto) 재사용.
  - `f"{public_base_url}/invest/stocks/{market}/{symbol}"` (symbol URL 인코딩).
  - crypto 심볼(`KRW-BTC`)도 그대로 인코딩 — `/invest/stocks/crypto/KRW-BTC` 라우트 존재 확인됨.

**죽은 링크 전수 교체 (이 PR 범위) — `build_position_detail_url` 공유 호출처 4곳 + crypto 인라인 1곳:**

가장 깔끔한 방법은 `build_position_detail_url` **구현 자체를 `/invest/stocks/...`로 교체**(또는 신규 함수로 리네임 후 import 교체)하여 아래 4 호출처를 한 번에 수정:

| 호출처 | 용도 | 조치 |
|---|---|---|
| `app/services/fill_notification.py:455` | 체결(format_fill_message) | 신 경로로 교체 (또는 포맷터 신설로 대체) |
| `app/services/openclaw_client.py:120` | n8n fill payload | **제거**(Phase 1에서 체결 경로 삭제) |
| `app/jobs/kis_trading.py:197` | 토스 가격추천 detail_url | 신 경로로 교체 |
| `app/services/toss_notification_service.py:207` | 토스 알림 detail_url | 신 경로로 교체 |

추가 별도 인라인 죽은 링크:
- `app/services/crypto_pending_order_alert_service.py:172` → `{base}/portfolio?market=crypto&symbol={symbol}` (역시 410). `/invest/stocks/crypto/{symbol}`로 교체.

> 이 교체는 모두 "죽은 링크 → 살아있는 링크"라 strictly better. Discord 알림 링크가 `/invest/stocks`로 **통일**된다. (토스/크립토펜딩 알림 메시지 본문 자체 개선은 비범위 — 링크만 수정.)

### 3-5. 최소금액 필터 통화 인식화

현재 `openclaw_client.send_fill_notification`의 `filled_amount < 50_000`을 새 경로로 옮기되 통화 인식:
- KRW: `< 50_000`
- USD: `< 50` (≈ 동등 수준, 구현 플랜에서 임계 확정)
- 통화 미상: 보수적으로 발송(스킵하지 않음) 또는 KRW 기준 — 구현 플랜에서 확정.

---

## 4. n8n 체결 경로 제거 (Phase 1 범위)

- `websocket_monitor._send_fill_notification`: `OpenClawClient.send_fill_notification` 호출 제거 → `TradeNotifier.notify_fill`로 교체.
- `app/services/openclaw_client.py`: `send_fill_notification` + `_build_n8n_fill_payload` 제거.
- 관련 테스트(체결 6종) 제거/대체.
- **유지(Phase 2로 이월)**: `OpenClawClient`의 나머지 메서드(`send_watch_alert_to_router`, `request_analysis`, `send_scan_alert`), `app/routers/n8n*.py`, `app/services/n8n_*`, `docker-compose.n8n.yml`, `n8n/` 디렉터리, config 키. 이들은 소비처 검증 후 별도 이슈에서 정리.
- `N8N_FILL_WEBHOOK_URL` config 키: 체결 경로 제거 후 무참조가 되면 이 PR에서 제거 가능. 단 `.env.prod.native`/`env.example` 정리는 operator 안내로 동반.

---

## 5. 테스트 계획

| 테스트 | 검증 |
|---|---|
| `notify_fill` 디스패치 | market_type→webhook 라우팅(kr/us/crypto), Telegram 미러, disabled 시 no-op |
| Discord 포맷터 | 매수/매도/부분체결 라벨, 색상, embed.url 링크, 슬리피지 표기/생략, enrichment 표기/생략 |
| Telegram 포맷터 | 동일 정보, 마크다운 링크, 한글명 통일 |
| `fill_enrichment` | KR/US 평단·수량·실현손익 근사치 계산 정확성, 조회 실패 시 None(fail-open) |
| 통화 임계 | KRW 50,000 / USD 임계 경계값, 통화 미상 처리 |
| 링크 빌더 | `/invest/stocks/{market}/{symbol}` 생성, crypto 심볼 인코딩, market 미상 시 None |
| websocket_monitor 배선 | n8n 대신 notify_fill 호출, 중복 ledger row면 미발송(기존), enrichment 예외가 알림을 막지 않음 |
| 회귀 | 죽은 `build_position_detail_url` 잔존 참조 0 (grep 가드 or 테스트) |

DB/브로커 의존은 단위 테스트에서 fake/mock. enrichment는 서비스 주입 가능하게 설계.

---

## 6. 안전 경계 / 비목표

- **브로커 mutation 없음**: 알림은 read-only. 주문/원장 변경 없음.
- **fail-open**: enrichment·링크·한글명 실패가 알림 발송을 막지 않는다.
- **마이그레이션 0**: 스키마 변경 없음.
- **비목표(Phase 2)**: 전체 n8n/OpenClaw 디커미션, watch-alert 경로 변경, crypto_pending_order_alert 통합, 부분체결 집계 정책 변경.

---

## 7. 오픈 이슈 (구현 플랜에서 확정)

1. crypto(Upbit) 평단·실현손익의 정확한 조회 지점.
2. `get_reference_prices` 평단이 체결 반영 전/후인지 → 매수 포지션 라벨 문구. + operator user_id 확보 방법, kis_holdings 스냅샷 재사용으로 추가 API 호출 회피.
3. USD 최소금액 임계 값, 통화 미상 정책.
4. ~~build_position_detail_url 호출처~~ → **해결**: 공유 4곳(fill/openclaw[제거]/kis_trading/toss_notification) + crypto_pending 인라인 1곳. §3-4 표 참조.
5. `N8N_FILL_WEBHOOK_URL` 키 제거 vs 보존(Phase 2까지) 결정.
6. enrichment 비용이 높을 경우 1차 릴리스에서 제외할지(슬리피지+한글명+링크만) — §3-2 대안.

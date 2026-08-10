# `halted_suspect` — 정지 의심 종목 (ROB-1236)

## 0. 한 줄

최근 **N=3 거래일 연속** 봉이 죽어 있으면(`volume=0` **또는** 0-변동) 그 종목은
`data_state: "halted_suspect"` 로 표시되고, **지표는 null 이 되며**, 스크리너·정책표에서
**제외**된다. 🔴 **이건 "의심"이지 확정 정지가 아니다.**

## 1. 왜 생겼나 (실사례)

`000880` 한화 — 인적분할 매매거래정지로 8거래일 연속 `volume=0`, OHLC 83,800 동결.
그런데 `analyze_stock_batch` 는 `data_state: "fresh"` + 정상 현재가 83,800 을 반환했고,
RSI 35.40 · 지지/저항 · upside +84% 가 **전부 0-변동 캔들 위에서 계산**됐다.
11:30 live 세션이 이 종목을 매수 후보 rank 2 로 랭킹했고, 13:05 후속 세션이 OHLCV 를
직접 교차확인해서야 정지를 발견해 **제안 직전에 차단**했다.

원인: `data_state` 판정이 「최신 봉이 있느냐」만 보고 **봉이 살아 있느냐**를 안 봤다.

## 2. 판정 규칙

한 봉이 **frozen** 인 조건 (둘 중 하나):

| 조건 | reason 코드 |
|---|---|
| `volume` 을 알 수 있고 정확히 `0` | `zero_volume` |
| `high == low == close` (open 이 있으면 `open == close`) **그리고** `close == 직전봉 close` | `zero_variation` |

**가장 최근 봉에서 끝나는** frozen 연속 구간이 **3개 이상**이면 `halted_suspect`.

- 🔴 두 번째 조건이 "직전 close 와 같을 것"을 요구하는 이유 = **상한가/하한가 잠김**도
  `open=high=low=close` 로 찍히지만 **직전 close 와는 반드시 다른 가격**이기 때문. 이 절이
  없으면 잠김 종목이 전부 정지로 오판된다.
- 이미 거래가 재개된 종목은 과거에 frozen 구간이 있어도 잡히지 않는다(최신 봉 기준).

### N=3 의 근거 (양쪽 오차)

- **N 이 크면**: 000880 은 8거래일이었지만 **오염된 매수 랭킹은 정지 중간에** 일어났다.
  실측치(8)에 맞추면 마지막 날에만 발화해 **실사례 자체를 못 막는다.** 게다가 조회공시 요구·
  불성실공시 지정예고·단일가 조치 같은 **1~3일짜리 짧은 정지는 전부 놓친다**(건수로는 이쪽이 다수).
- **N 이 작으면**: `N=1` 은 거래 한산한 초소형주의 하루 무거래, 그리고 **volume 적재 실패로
  0 이 들어간 행**까지 전부 잡는다. `N=2` 도 2행짜리 적재 갭에 발화한다.
- **N=3 에서 남는 오차**:
  - 위음성 — **1~2일 정지는 안 잡힌다.** 그 구간은 여전히 세션까지 흘러가고, 운영 세션의
    OHLCV 교차확인이 계속 백스톱이다.
  - 위양성 — 정말로 3일 연속 무거래인 초저유동 종목은 잡힌다. 스크리너·정책표는 이미
    turnover 하한을 걸어 이런 종목을 거르고, **제외는 전부 심볼·근거와 함께 보고**되므로
    조용히 사라지지 않는다.

## 3. KRX 거래정지 마스터

🔴 **없다.** 이 레포에는 거래정지 마스터 테이블도, 서비스도, 수집 CLI 도 없다. 따라서
판정은 **봉의 모양만** 근거로 하며, 응답에 `krx_halt_master: "unavailable"` 를 항상 적어
"마스터를 봤다"는 오해를 차단한다. 🔴 **확정 정지로 단정해서 보고하지 말 것.**

## 4. 소비자별 동작

| 소비자 | 동작 |
|---|---|
| `analyze_stock_batch` / `analyze_stock` | `data_state` = `halted_suspect` (top-level + quote 양쪽). `indicators` · `support_resistance` = **null** (추정·보간 없음). recommendation 은 RSI 부재로 `hold`/`low` + `insufficient_inputs` 로 자동 하한. 근거는 `halt_suspect` 블록. compact 요약에도 실린다 |
| `screen_stocks` | 행 **제외**. 제외분은 `meta.halted_suspect_excluded` + `warnings` 로 보고. 🔴 봉 이력 조회 실패는 **제외하지 않고**(fail-open) warning 만 남긴다 — DB 장애는 정지의 증거가 아니다. 🔴 비용 게이트 = §4-1 |
| `scripts/policy_table` (kr/us/crypto) | `rows` 에서 **제외**. `universe.halted_suspect` 에 심볼·근거 보존, KR/US 는 `universe.skipped` 에 `reason="halted_suspect"` 로도 계상. 요약 md 에 심볼이 그대로 찍힌다 |

B0-X 는 `policy_table.v1` 의 `rows` 만 읽으므로 별도 변경 없이 오염이 차단된다
(`universe.halted_suspect` 는 additive, `schema` 값 불변).

### 4-1. 스크리너 비용 게이트 (🔴 알려진 한계)

봉 이력 조회는 cache-first 이지만 **KRX 장중에는 일봉 캐시를 일부러 우회**한다(오늘 봉이
형성 중이라). 그대로 두면 100행 스크린이 장중에 **KIS 라이브 캔들 100회**를 때린다 —
운영 세션이 도는 바로 그 시간대에. 그래서 이력 조회는 **행 자신의 최신봉 거래량**으로
먼저 거른다: 판정은 "최신봉에서 끝나는" 연속 구간을 요구하므로, 최신봉이 거래됐다면
`zero_volume` 정지는 성립할 수 없다. `volume` 이 없거나 파싱 불가면 **거르지 않고
이력을 읽는다**(게이트가 정지를 놓치는 원인이 되면 안 되므로).

🔴 **이 게이트가 감수하는 구멍**: 거래량은 있는데 **0-변동만으로** 얼어붙은 구간
(3일 연속 장중 변동폭 0 + 종가 불변)은 스크리너에서 건너뛴다.
`analyze_stock_batch` 와 정책표 빌더는 이력을 **무조건** 읽으므로 그쪽에서는 잡힌다 —
이 단축은 스크리너 행별 hot path 에만 있다.

## 5. 운영자 대응

1. `halted_suspect` 가 뜨면 **먼저 실제 정지 여부를 직접 확인**한다(KRX/증권사 공시).
2. 진짜 정지 → 그대로 두고 매매 대상에서 뺀다.
3. **오탐이면(거래는 되는데 3일 연속 무거래·무변동)** → 표·스크리너에서 빠진 게 맞는지
   `meta.halted_suspect_excluded` / `universe.halted_suspect` 로 확인하고, 필요하면 해당
   종목만 수동으로 다시 검토한다. 🔴 임계값을 낮추기 전에 오탐 사례를 먼저 기록할 것.

## 6. 코드 위치

- 판정기(순수, stdlib): `app/services/halt_detection.py`
- analyze 배선: `app/mcp_server/tooling/analysis_analyze.py::_apply_halt_suspect`
- 스크리너 게이트: `app/mcp_server/tooling/screening/halt_filter.py`
  (`screen_stocks_unified` 단일 깔때기에서 호출)
- 정책표: `scripts/policy_table/adapters/{kr,us,crypto}.py::compute_policy_table`
- 테스트: `tests/services/test_halt_detection.py`,
  `tests/mcp_server/test_analyze_halted_suspect.py`,
  `tests/mcp_server/tooling/test_screen_stocks_halt_filter.py`,
  `tests/scripts/test_policy_table_halt_suspect.py`

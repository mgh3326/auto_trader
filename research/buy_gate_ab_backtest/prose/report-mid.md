
---

## 2. look-ahead 부재 증명

**주장: 지표는 결정일까지의 봉으로만 계산된다.**

증명 절차는 세 겹이고, 세 겹 모두 테스트로 기계 검증된다
(`research/buy_gate_ab_backtest/tests/`, 10 passed).

1. **입력 슬라이스** — 증거 생성기는 `bars = group.iloc[: pos + 1]` 로만
   호출된다. `pos` 는 결정 세션의 위치이므로 결정일 **이후 행은 함수에 들어가지
   않는다.** RSI는 그 슬라이스의 마지막 250봉, 지지선은 마지막 60봉이다.
2. **경계 자체를 강제** — 🔴 **B1 수리**. 1차 보고의
   `test_no_future_bar_can_reach_the_evidence` 는 **결함이었다**: `past_only` 와
   `with_future` 를 **같은 슬라이스에서** 만들어 서로 비교했기 때문에 미래 봉이
   들어간 입력을 단 한 번도 함수에 넘기지 않았고, 따라서 **실패할 수가 없는
   테스트**였다. 적대검증이 러너의 `group.iloc[:pos+1]` 을 `[:pos+2]` 로 바꿔도
   12개 테스트가 전부 green 임을 보여 이를 입증했다.

   지금은 `build_evidence` 가 **`decision_date` 를 필수 인자로 받고**, 입력의
   마지막 세션이 결정일과 다르면 `LookAheadError` 로 **fail-closed** 한다.
   caller 슬라이스가 유일한 방어였던 상태를 끝냈다.
3. **러너 실경로 관통 테스트** — `test_runner_slice_is_covered_by_the_look_ahead_guard`
   는 합성 패널로 **`run_market` 자체를 호출**해(스텁은 코퍼스 로더뿐, 슬라이스는
   프로덕션 코드) 모든 표본의 진입가가 **결정일 당일 종가**임을 검사한다.

4. **채점 쪽 차단** — 전방 창은 사전등록 `score_window` 의 `_usable_bars` 가
   열고, 그 함수는 `session_date <= decision_date` 인 봉을 **전부 버린다.**
   `test_scoring_ignores_bars_at_or_before_the_decision_date` 가 이를 고정한다.

### 뮤턴트 검출 실측 (B1 수리 증명)

| 뮤턴트 | 결과 |
|---|---|
| 없음 (원본) | `13 passed` |
| 러너 `[:pos+1]` → `[:pos+2]` | **`1 failed`** — `LookAheadError: evidence input ends 2018-12-18, decision session is 2018-12-17` |
| 위 + `build_evidence` 가드 조건 무력화 | **`2 failed`** — 러너 관통 테스트가 가드와 **독립적으로** 잡는다 |
| 원복 | `13 passed` |

원복 증명 — `run_backtest.py` 의 변조 전 / 원복 후 SHA-256 동일:

```text
6091cb7eaee727fae811dbdbc39dd6a912bf9341fd0f889ff061f158f5383dcb
```

**추가로 미래를 끌어올 수 있었던 통로 두 개, 둘 다 막았다:**

- **유동성 필터**: `rolling(20).median()` — 과거 20봉만 본다. 전체 기간
  평균 거래대금 같은 전역 통계로 유니버스를 고르지 않았다.
- **채점 as-of**: 시장별 **단일** as-of(코퍼스 마지막 세션)를 쓴다. 표본별로
  "충분한 미래가 있는 날짜만" 고르지 않았다. 그래서 코퍼스 끝자락 표본은
  `scoreable=false` 로 남는다 — 보간하지 않고 그대로 뺀다(표에 n(제출)과
  n(채점가능)을 나란히 실은 이유).

**네트워크·DB 0 증명**: 테스트 실행 시 소켓 가드가
`active=True blocked_attempts=0` 을 보고했다(시도 자체가 0). 앱 `Settings`
싱글턴은 `reconstruct.py` 가 첫 app import 전에 **무효 플레이스홀더**로 채우므로
운영 자격증명이 프로세스에 로드되지 않는다. 코퍼스 로더는 경로에 `holdout` 이
들어가면 예외를 던진다.

---

## 3. 표본 구성 규칙 (결과 보기 전에 고정, digest 핀)

`research/buy_gate_ab_backtest/preregistration.py`,
`addendum_sha256()` = `648005cb…55da`, 테스트로 고정.

- **결정일**: 코퍼스 세션 캘린더의 **매 5번째 세션**(주 1회 케이던스). 위상은
  튜닝하지 않고 "250봉 이력이 처음 채워지는 세션"으로 고정.
  🔴 **S3 정정**: 1차 보고의 결정일 수(KR 492 / US 453)는 **선행 250세션이 없어
  표본을 하나도 만들 수 없는 최초 50개 phase date 를 포함한 과대 표기**였다.
  실제 eligible 결정일은 **KR 442 / US 403**이다. 표본 자체는 올바른 phase 에서
  시작했으므로(첫 표본일이 union-calendar index 250, `250 % 5 == 0`) **수치는
  영향을 받지 않고 표기만 정정**된다. 결과 파일은 이제 `decision_sessions`
  (eligible) 와 `phase_sessions_total` (원 grid) 를 나눠서 싣는다.
- **유니버스**: 결정일 기준 250봉 이상 + 20세션 중위 거래대금이 하한 이상
  (KR 10억원 / US $5M / upbit 10억원 / binance $5M). **상위 N 선별 없음, 캡 없음.**
- **지지선 1개 선택 규칙**: 현재가 아래 8% 이내 중 **가장 강한** 것, 동률은 근접.
  🔴 이 규칙은 **어느 변종의 강도 임계값도 참조하지 않는다** — 참조하면 A와 B가
  서로 다른 레코드를 보게 되어 대칭이 깨진다.
- **상수 4종**(cadence 5 · 유동성 lookback 20 · RSI 창 250 · 지지 창 60)은
  🔴 **S2 정정**으로 addendum dict 안으로 옮겨 **digest 가 덮도록** 했다. 값은
  최초 freeze 커밋 `c9a3270` 과 동일하며(git blame 이 증거) 위치만 이동했다.
  이전 digest `648005cb…55da` 는 `FIRST_FREEZE_ADDENDUM_SHA256` 로 보존된다.
- **계산 단축**(사전 고정): RSI ≥ 45 면 공유 게이트가 **양쪽 모두** 이미 기각하므로
  지지선 재구성을 생략한다. 코호트 배정 불변임을 테스트로 증명
  (`test_rsi_shortcut_is_cohort_neutral`). 대신 대조군의 강도 히스토그램은
  RSI 통과분만 덮으므로 `not_computed` 로 표시된다.

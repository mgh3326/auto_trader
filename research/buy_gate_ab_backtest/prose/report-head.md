# ROB-1301 매수 게이트 A/B — 역사 백테스트 1차 보고

- 작성: gatebt 워커 · 2026-08-21
- 지시: claude-mock (상류 조정, wB:p4R) — §129차 ② 후속
- 산출 코드: `research/buy_gate_ab_backtest/` (draft PR, `app/` 변경 0)
- 원시 결과: `~/work/herdr-artifacts/gatebt-ab-20260821/v2/{kr,us,crypto_*}.json`
- 코드 커밋: `fd8f63f` (PR #1930, draft)

---

## 🔴 이 문서의 사용 제한 (머리에 못 박음)

1. **이 결과는 적대검증 1라운드 전에는 정책 결정 근거로 쓰지 않는다.** 아래 어떤
   숫자도 게이트 문언 변경·승격·자동화의 논거가 될 수 없다.
2. **정책 권고 없음.** 이 문서는 데이터 요약에서 끝난다. "A가 옳다/B가 옳다"는
   판정을 하지 않으며, 그런 문장은 이 문서에 없다.
3. **라이브 셰도우를 대체하지 않는다.** ROB-1301 라이브 4주 수집이 정본이고,
   이것은 같은 질문에 대한 **과거 표본 선행 답**일 뿐이다.
4. **봉인 홀드아웃 미개봉.** 세 코퍼스 모두 2025-01-01…2026-07-31 홀드아웃을
   한 바이트도 읽지 않았다. 따라서 **최근 레짐은 이 백테스트에 없다.** 확장은
   운영자 결정 사항이다.
   🔴 **S5 — 증거 표면은 시장마다 다르다**(하나로 뭉뚱그리지 않는다):

   | 코퍼스 | 미개봉 증거 |
   |---|---|
   | KR | `holdout-access.log` — WRITE 2줄 · **READ 0줄** · `final_holdout_data_reads=0` |
   | US | `holdout-access.log` — 2025/2026 partition WRITE 2줄 · **READ 0줄** |
   | crypto | 🔴 **access log 파일이 없다.** corrected manifest 의 `validation.holdout_files_opened=0` (+ `staging_files_opened=0`) 가 증거 |

   코드 쪽 차단은 별개로 `corpus._assert_not_holdout` 이 parquet read loop
   **이전에** 걸리며, 합성 `/holdout/` 경로 주입 시 `RuntimeError` 로 거부된다.

---

## 1. 무엇을 그대로 썼고, 무엇을 못 썼나

### 1.1 재사용 (발명 금지 준수)

| 입력 | 권위 있는 출처 | 재구현 | 변조 |
|---|---|---|---|
| A/B 판정·코호트 | `buy_gate_ab_shadow.evaluate.evaluate_candidate` | 없음 | 없음 |
| 표본 단위 수익·MDD | `buy_gate_ab_shadow.scoring.score_window` | 없음 | 없음 |
| 지지선·강도 | 라이브 `get_support_resistance_impl` (preloaded frame) | 없음 | 🔴 **module-global 몽키패치 1건** ↓ |
| RSI(14) | 라이브 `_compute_indicators` | 없음 | 없음 |

🔴 **S1 — 몽키패치 명시**: 함수 바이트는 무변경이지만 그 함수의 **global 의존성**
하나를 대체한다.

```python
_sr.fetch_us_live_last_price = _no_live_price   # reconstruct.py, import 시점
```

`get_support_resistance_impl.__globals__['fetch_us_live_last_price']` 가 이
스텁으로 해소된다. 백테스트에 인트라데이 호가가 존재하지 않으므로 US 분기가
소켓을 열지 않고 KR/crypto 와 같은 **종가 분기**를 타게 하는 것이 목적이며,
결과적으로 US 는 "종가 시점 판단" 가정을 갖는다. 즉 정확한 표현은
"재구현 없음"이지만 **"무변조"는 아니다** — US 는
**공식 함수 + 그 함수의 global 의존성 1개 대체**다.

그 밖의 입력 덮어쓰기(모두 A/B 공통, §1.2): `honest_upside_pct`,
`other_gate_bits` 3종, support frame 60봉 / RSI frame 250봉 슬라이스.

`app/`는 **읽기만** 했다(변경 0). 사전등록 스펙 digest
`spec_sha256()` = `a2814c87…e672` 를 결과 파일마다 박아 두었다.

지지 강도는 별도 프록시를 발명하지 않았다. 라이브 `get_support_resistance_impl`
자체가 **60봉 OHLCV만의 순수 함수**(피보나치 + 볼륨 프로파일 POC/밸류에어리어 +
볼린저 3선 → 2% 클러스터링 → 클러스터 내 **서로 다른 source 개수** ≥3=strong,
2=moderate, 1=weak)이기 때문에, 과거 봉으로 그대로 호출하면 라이브와 **같은
코드가 같은 라벨**을 만든다.

**프록시성이 남는 지점**(정직하게):
- 라이브는 세션의 analysis 페이로드에서 강도를 읽지만 여기선 봉에서 재계산한다.
- 라이브 **US 분기는 인트라데이 호가로 current_price를 덮어쓴다.** 코퍼스에
  인트라데이가 없으므로 그 오버라이드를 끄고 KR/crypto와 같은 종가 분기로
  고정했다. → US 결과는 "종가 시점 판단" 가정이 들어간다.

### 1.2 재구성 불가 → **A·B 동일 중화**

| 사전등록 게이트 | 왜 불가 | 처리 |
|---|---|---|
| `honest_upside_pct ≥ 40` | 시점별 애널리스트 목표주가 컨센서스가 어느 코퍼스에도 없음 | 양쪽 동일 pass 중화 |
| `other_gate.liquid_midcap` | 시점별 시총·유동주식수 없음 | 양쪽 동일 pass 중화 (유니버스 유동성 하한이 부분 대역) |
| `other_gate.concentration` | 포트폴리오 상태 의존 — 백테스트에 포트폴리오가 없음 | 양쪽 동일 pass 중화 |
| `other_gate.overhang` | 오프라인 확정 정의 부재 | 양쪽 동일 pass 중화 |

🔴 **중화의 방향성**: 공유 게이트를 중화하면 A와 B가 **같은 규칙으로 같이**
커진다. 따라서 **A-vs-B 대비(=측정 대상)는 살아남지만, 모든 절대 admit 건수는
상한(upper bound)** 이다. "실제로 하루 이만큼 후보가 나온다"로 읽으면 안 된다.

**추가로 적용하지 않은 것**: 라이브 `buy_candidate_fanout`의
`독립 지지 family ≥ 2` 게이트는 ROB-1301 사전등록 `shared_gates`에 **없다.**
넣으면 사전등록에 없던 게이트를 추가하는 것이므로, 계산은 하되 **부록 기록으로만**
두고 게이트로 쓰지 않았다.

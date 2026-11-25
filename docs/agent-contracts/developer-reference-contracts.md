# 개발 참고 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- 개발 환경 설정
- 환경 변수
- 테스트 작성
- 문제 해결
- 참고 문서
- Exact-SHA deployment gate (ROB-1456)

## 기준 원문 계약

## 개발 환경 설정

플릿 세션(스폰된 워커, 디자인 세션, 리드 세션)은 테스트·DB·호스트 규칙에 스폰 프리앰블의 `Host safety (fleet-v1)` 블록을 따른다. 아래 명령은 사람과 CI를 위한 것이다.

### 필수 요구사항
- Python 3.13+
- UV (의존성 관리)
- PostgreSQL (데이터베이스)
- Redis (캐싱·토큰 관리·TaskIQ 브로커)

### 초기 설정
```bash
# UV 설치 (미설치 시)
pip install uv

# 의존성 설치
uv sync                           # 프로덕션 의존성만
uv sync --all-groups              # 모든 의존성 (test, dev 포함)

# 환경 변수 설정
cp env.example .env
# .env 파일 편집하여 API 키 설정

# 데이터베이스 마이그레이션
uv run alembic upgrade head

# 개발 서버 실행
make dev                          # 또는 uv run uvicorn app.main:app --reload
```

### Docker 환경
```bash
docker compose up -d              # PostgreSQL, Redis, Adminer 시작
docker compose ps                 # 서비스 상태 확인
docker compose down               # 서비스 중지
```

## 환경 변수

**필수 환경 변수 (.env 파일):**

```bash

# 한국투자증권 (KIS)
KIS_APP_KEY=xxx
KIS_APP_SECRET=xxx
KIS_ACCOUNT_NO=12345678-01            # 선택사항

# Upbit
UPBIT_ACCESS_KEY=xxx
UPBIT_SECRET_KEY=xxx
UPBIT_BUY_AMOUNT=100000               # 분할 매수 금액 (기본 10만원)
UPBIT_MIN_KRW_BALANCE=105000          # 최소 KRW 잔고

# 데이터베이스
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname

# Redis (방법 1: URL로 통합 설정 - 권장)
REDIS_URL=redis://localhost:6379/0

# Redis (방법 2: 개별 설정 - REDIS_URL이 없을 때만 사용)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=                       # 선택사항
REDIS_SSL=false

# Telegram (선택사항)
TELEGRAM_TOKEN=xxx
TELEGRAM_CHAT_IDS_STR=chat_id1,chat_id2

# OpenDART (선택사항)
OPENDART_API_KEY=xxx

# TradingAgents advisory runner (ROB-9, 선택사항)
TRADINGAGENTS_REPO_PATH=/path/to/TradingAgents
TRADINGAGENTS_PYTHON=/path/to/TradingAgents/.venv/bin/python
TRADINGAGENTS_RUNNER_PATH=/path/to/run_auto_trader_research.py
# 전체 설정과 안전 제약은 docs/plans/ROB-9-tradingagents-auto-trader-integration-plan.md 참고
```

## 테스트 작성

플릿 세션(스폰된 워커, 디자인 세션, 리드 세션)은 테스트·DB·호스트 규칙에 스폰 프리앰블의 `Host safety (fleet-v1)` 블록을 따른다. 아래 명령은 사람과 CI를 위한 것이다.

**테스트 마커 사용:**
```python
import pytest

@pytest.mark.unit
async def test_analyzer_prompt_generation():
    """단위 테스트"""
    pass

@pytest.mark.integration
async def test_upbit_api_integration():
    """통합 테스트 (실제 API 호출)"""
    pass

@pytest.mark.slow
async def test_heavy_operation():
    """느린 테스트"""
    pass
```

**테스트 실행:**
```bash
pytest tests/test_file.py -v -k "test_name"  # 특정 테스트
pytest tests/ -v -m "not integration"        # 통합 테스트 제외
pytest tests/ -v -m "not slow"               # 느린 테스트 제외
```

## Exact-SHA deployment gate (ROB-1456)

`scripts/deploy-ncp-pull.sh` forward deploys and dry-runs call the stdlib-only
`scripts/deploy_ci_gate.py` before mutation. Remote OCI revision identifies the
exact commit; the verified registry digest is what is pulled and promoted.
GitHub's newest exact-SHA `Test` push/dispatch run and latest attempt must have
`lint`, `taskiq-smoke`, and all four `test (3.13, N)` jobs completed successfully.
Missing/red/skipped/unreadable evidence refuses with exit 78. ROB-1454's
`PR gate` is not full-suite evidence. The image build workflow enforces the
same gate on its pinned build SHA and follows completed main-push Test runs.

`--allow-unverified REASON` is an explicit logged CI override, not permission
to bypass immutable image identity or other safety checks. `--rollback` and
automatic failure recovery stay unchecked emergency paths. Install the helper
beside the host script; Python >= 3.11 (via `AT_DEPLOY_GATE_PYTHON`, uv, or python3; older refuses 78), Docker Buildx, and authenticated GitHub CLI
Actions read access are prerequisites. Runbook:
`docs/runbooks/ncp-pull-deploy.md` §Exact-SHA full-suite gate.

## Content-based rollout approval

`.mergewake/policy.yaml` defines `deploy_approval` for live authority, production
migrations, scheduler/flow enablement and trading defaults. The rollout controller
checks the previous deployed policy against the exact deployed..release range.
Matched or unknown ranges hold for an exact human-only hk decision; dedicated
mock/paper and telemetry-only paths remain automatic. Environment and secret
changes are never automatic. This policy does not relax the exact-SHA CI gate,
register schedules, enable trading switches or authorize an order.

## pi 자동 배포 (hk 6037)

pi(shiftgate)의 AT 앱은 `main` 커밋이 스스로 배포된다. 흐름: main push → Test(전체 스위트) → `deploy.yml` 이
`ghcr.io/mgh3326/auto_trader:sha-<7>` 빌드 → `promote` 잡(`promote-deploy.yml`, `scripts/ci/promote_deploy.py`)이 `deploy` 태그를
같은 digest 로 옮김(재빌드 없음) → pi 의 systemd 타이머가 `deploy/shiftgate/pi/follow.sh` 를 실행 → 백업 → 기존
`deploy/shiftgate/pi/deploy.sh sha-<7>`. 🔴 pi 로 들어오는 경로(ssh·터널·배포 키)는 없다. 🔴 `shiftgate follow` 는 여전히 쓰지 않는다
(게이트를 우회한다). 타이머 파일은 레포에 있으나 **설치·enable 은 운영자(설계 세션)만 한다** — 레포 코드가 스스로 타이머를 설치하지 않는다.

- **승격 판정(promote)**: 후보 SHA 가 main 의 조상이고 전체 스위트 6개 job 이 초록(`deploy_ci_gate.py --main-ancestor --expect-sha`)이며
  이미지 OCI revision 이 그 SHA 일 때만, 그리고 배포 중 커밋(`deploy` 태그의 SHA)과 후보 사이가 mergewake 롤아웃의 `deploy_approval` 규칙에서
  보류가 아닐 때만 태그를 옮긴다(정책은 **배포 중 커밋**의 것을 읽는다·정책 파일/환경·비밀 변경·알 수 없는 배포 중 커밋·갈라진 이력·300개 파일 비교 한계·
  읽을 수 없는 정책은 보류). 이 레포의 규칙으로 `alembic/versions/**`·`alembic/env.py` 변경과 판정·설치 도구 자체(`promote_deploy.py`·`deploy_ci_gate.py`·`promote-deploy.yml`·`deploy.yml`·`deploy/shiftgate/pi/**`) 변경은 정책 문구와 무관하게 항상 보류한다(mergewake 규칙의 완화가 아니라 추가 규칙). 🔴 판정 도구는 후보의 트리가 아니라 신뢰 ref(워크플로가 체크아웃한 main HEAD, `github.sha`)에서 실행되고 후보는 `--sha` 로만 들어간다 — 릴리스가 자기를 판정하는 코드를 바꿔 자기 게이트를 풀 수 없다(`tests/ci/test_promote_deploy_workflow.py`). `promote-approved` 는 자동 promote 와 별도 concurrency 그룹(후보 SHA 별)이라 승인 실행이 다음 main push 에 조용히 취소되지 않고, 태그를 옮기기 직전 `deploy` 태그를 다시 읽어 달라졌으면 옮기지 않는다. 보류는 잡을 빨갛게 하지
  않고 태그를 옮기지 않으며 잡 요약(+Discord)에 사유·경로·재승격 명령을 남긴다. 승인은 hk 결정으로 받고 `promote-deploy.yml` 을 `workflow_dispatch`
  (`promote_sha`, `approval_ref`)로 다시 실행한다. `approval_ref` 가 있는 실행은 required reviewers 가 설정된 Environment `at-deploy-approval` 의 `promote-approved` 잡에서만
  돌고 스크립트는 그 밖의 `approval_ref` 를 거부한다 — 승인은 범위 보류만 면제하고 CI·main 조상·이미지 확인은 그대로다. 🔴 `scripts/ci/promote_deploy.py` 의 정책 읽기·
  글롭은 mergewake 와 같은 규칙(픽스처 `tests/fixtures/mergewake_deploy_approval_paths.json` 로 고정)이며 더 느슨하게 만들지 않는다.
- **follow(pi)**: `deploy` 태그 → digest → SHA 를 레지스트리에서 읽고, digest 가 `/root/at-run/deployed-digest` 와 같으면 조용히 끝난다(GitHub 호출 없음).
  바뀌었으면 태그를 옮긴 쪽을 믿지 않고 스스로 `deploy_ci_gate.py --github anonymous --main-ancestor` 로 다시 확인한다(공개 레포의 인증 없는 GitHub 읽기, 시간당 60회).
  호스트 배포 잠금(`host-lock.sh`) → `systemctl start at-pg-backup.service`(성공해야만 진행) → `deploy.sh sha-<7>`(`--allow-unverified` 는 이 경로에 없다).
  방금 검증한 digest 는 `AT_FOLLOW_VERIFIED_DIGEST` 로 deploy.sh 에 넘겨 같은 틱에 GitHub 를 두 번 묻지 않는다(deploy.sh 는 태그가 그 digest 인지만 레지스트리에서 확인하고, 변수가 없거나 어긋나면 전체 게이트 — 수동 실행은 불변).
  거부·실패한 digest 는 `/root/at-run/follow-hold` 로 묶여 매 틱 재시도하지 않는다. 게이트 자신의 문구(`GitHub evidence unavailable`·`unverified (`)로 알 수 있는 일시 장애만 retry-after 뒤 digest 마다 최대 3번 다시 시도하고(`follow-transient`) 그 뒤 영구 hold 다. 성공한 백업은 digest 별로 기록해 일시 장애 재시도에서 다시 돌리지 않는다(`follow-backup-ok`, 1시간 이내). 결과는 `follow.log`·`follow-status.json`
  (+ 있으면 panewire `info`)에 남는다. 롤백은 `follow.sh --hold` 후 `deploy.sh --rollback`.
- **신뢰 경계**: `deploy` 태그 쓰기 권한(Actions `GITHUB_TOKEN`, GHCR 패키지 쓰기)은 신뢰 경계다. follow 의 재확인은 "main 조상 + 전체 스위트 초록"까지다.
  hk 쪽 자동 재승격(결정에 A 로 답하면 dispatch)은 후속 작업이다. 런북: `docs/runbooks/pi-shiftgate-deploy.md` §4.1.
- **이미지 정리 (hk 6437)**: 성공한 전진 배포(수동·`follow.sh` 모두)는 `deployed-digest` 를 쓴 뒤 오래된 `auto_trader` 이미지를 정리한다. 현재·이전 digest 와 컨테이너가 쓰는 이미지는 남긴다. 끄는 스위치는 `AT_IMAGE_PRUNE_ENABLED`(기본 1)이며 규칙은 `docs/runbooks/pi-shiftgate-deploy.md` §4.2 에 있다. 롤백·실패 경로는 정리하지 않는다.
- **테스트**: `tests/ci/test_promote_deploy.py`, `tests/ci/test_promote_deploy_workflow.py`, `tests/scripts/test_pi_follow.py`, `tests/scripts/test_deploy_ci_gate_anonymous.py`.

## 문제 해결

플릿 세션(스폰된 워커, 디자인 세션, 리드 세션)은 테스트·DB·호스트 규칙에 스폰 프리앰블의 `Host safety (fleet-v1)` 블록을 따른다. 아래 명령은 사람과 CI를 위한 것이다.

### KIS 분봉 API 문제
- **증상:** `time_unit` 파라미터가 제대로 작동하지 않아 모든 시간대에서 동일한 데이터 반환
- **대응:** 1분봉을 받아 로컬에서 집계한다(`app/services/brokers/kis/_base_market_data.py::_aggregate_minute_candles_frame`).

### Redis 연결 실패
- Docker Compose로 Redis 실행: `docker compose up -d redis`
- 연결 테스트: `docker compose exec redis redis-cli ping`
- 로그 확인: `docker compose logs redis`

### 데이터베이스 마이그레이션 충돌
```bash
# 현재 버전 확인
uv run alembic current

# 특정 버전으로 롤백
uv run alembic downgrade <revision>

# 마이그레이션 히스토리 확인
uv run alembic history
```


## 참고 문서

프로젝트 루트의 다음 문서들을 참고하세요:

- `docs/archive/JSON_ANALYSIS_README.md` — (아카이브·과거) 삭제된 Gemini analyzer 시절 JSON 분석 문서, 현행 아님
- `docs/archive/ANALYSIS_REFACTOR_README.md` — (아카이브·과거) 삭제된 analyzer/Redis 모델제한 시절 문서, 현행 아님
- `STOCK_INFO_GUIDE.md` - 데이터베이스 정규화 구조 및 SQL 쿼리 패턴
- `UPBIT_WEBSOCKET_README.md` - Upbit WebSocket 실시간 시세
- `DEPLOYMENT.md` - 배포 가이드
- `DOCKER_USAGE.md` - Docker 사용법

## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 루트 `AGENTS.md`의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.

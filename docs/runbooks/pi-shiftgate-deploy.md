# pi 배포를 shiftgate 로 (hk 5617)

pi(Debian 12, aarch64)의 AT 앱 컨테이너를 `shiftgate`(https://github.com/mgh3326/shiftgate, 고정 커밋 `f8a11c6`)와 어댑터 스크립트로 배포한다. hk 5753 이후 배포 대상은 세 개다(`at-worker` 는 은퇴, 아래 「hk 5753 — at-worker 은퇴」).

| 컨테이너 | 방식 | 노출 |
|---|---|---|
| `at-api` | shiftgate blue/green (`shiftgate-at-api-blue/green`, 색 포트 18000/28000, 호스트 네트워크) | HAProxy `127.0.0.1:8000` (cloudflared 터널 앞단) |
| `at-mcp` | shiftgate blue/green (`shiftgate-at-mcp-blue/green`, 색 포트 18765/28765, 호스트 네트워크) | HAProxy `100.90.137.33:8765` (tailnet 전용) |
| `at-upbit-ws` | shiftgate 범위 밖 싱글턴(HTTP 아님 — shiftgate docs/design.md "gap"). `swap-singletons.sh` 가 이름 보관·정지·새 run·상태 확인·실패 시 복원 | 없음 |

레포 파일(운영자가 호스트에 복사한다. 레포는 아무것도 설치·실행·스케줄하지 않는다 — 타이머도 설계 세션이 §4.1 절차로 설치한다):

- `deploy/shiftgate/pi/services.yaml` — 최종 설정. `/etc/shiftgate/services.yaml`.
- `deploy/shiftgate/pi/services.cutover.yaml` — 컷오버 1단계 전용. `services.yaml` 과 `binds` 두 줄(임시 포트 8100/8865)만 다르다.
- `deploy/shiftgate/pi/deploy.sh <tag>` / `deploy.sh --rollback` — 일상 배포와 복구의 유일한 진입점.
- `deploy/shiftgate/pi/swap-singletons.sh <image@digest>` — `deploy.sh` 가 부르는 싱글턴 교체(직접 부르지 않는다).
- `deploy/shiftgate/pi/host-lock.sh` — 배포 스크립트들이 source 하는 호스트 배포 잠금.
- `deploy/shiftgate/pi/follow.sh` + `at-follow.service` / `at-follow.timer` — 자동 배포(hk 6037, §4.1). `deploy` 태그가 새 digest 를 가리키면 스스로 다시 검증한 뒤 `deploy.sh` 로 설치한다. 설치 전 DB 백업은 없다(hk 6466) — 백업은 Dagu 일일 잡 `at-pg-backup`(매일 04:30 KST, `deploy/dagu/at-maintenance/at-pg-backup.yaml`)이 맡는다.

규칙: shiftgate 자체의 `follow` / `check` / `prune` 타이머는 등록하지 않는다(`shiftgate follow` 는 `deploy_ci_gate.py` 의 정확한 SHA 전체 스위트 게이트와 승인 보류를 우회한다). 자동 배포는 그 게이트를 앞에 둔 레포의 `follow.sh` 하나뿐이고(hk 6037, §4.1) 설치·enable 은 운영자(설계 세션)만 한다. 레포 코드는 어떤 타이머도 스스로 설치하지 않는다. `MCP_PROFILE=default` 는 `services.yaml` 에 명시되어 있어야 한다(#1189). `deploy_ci_gate.py` 와 은퇴 컨테이너 검사 규칙(ROB-1436)은 완화하지 않는다. env 파일은 경로로만 참조하고 열거나 출력하지 않는다.

이 문서에서 **[live check]** 는 레포에서 확인하지 못해 pi 에서 직접 확인해야 하는 값이다.

## 1. 1회성 호스트 준비

모두 pi 에서 root 로 한다. 이미 있으면 건너뛴다.

HAProxy 는 2.8 이상이 필요하다. Debian 12 기본 패키지는 2.6 이라 backports 를 쓴다 **[live check: 설치된 버전]**:

```bash
apt-get install -y -t bookworm-backports haproxy
haproxy -v
systemctl enable --now haproxy
```

기본 설정에는 바인드가 없으므로 이 시점에는 어떤 포트도 열리지 않는다. shiftgate 바이너리(Go 1.27 이상 필요, 빌드는 pi 에서 하거나 다른 머신에서 `GOARCH=arm64` 로 교차 빌드해 복사):

```bash
git clone https://github.com/mgh3326/shiftgate /root/src/shiftgate
git -C /root/src/shiftgate checkout f8a11c6
cd /root/src/shiftgate && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags='-s -w' -o shiftgate ./cmd/shiftgate
install -m 0755 /root/src/shiftgate/shiftgate /usr/local/bin/shiftgate
shiftgate --config /etc/shiftgate/services.yaml status
```

어댑터 스크립트와 게이트를 한 디렉터리에 설치한다(`deploy.sh` 는 `deploy_ci_gate.py` 를 옆에서 찾는다). 게이트에는 Python 3.11 이상, `docker buildx`, 인증된 `gh` 가 필요하다 **[live check]**:

```bash
install -d -m 0755 /root/at-run/shiftgate-pi
install -m 0755 deploy/shiftgate/pi/deploy.sh deploy/shiftgate/pi/swap-singletons.sh deploy/shiftgate/pi/host-lock.sh deploy/shiftgate/pi/follow.sh scripts/deploy_ci_gate.py /root/at-run/shiftgate-pi/
python3 -c 'import sys; print(sys.version_info >= (3, 11))'
gh auth status
```

GHCR 읽기 권한(`read:packages` 토큰만)이 이 사용자의 Docker 로그인에 있는지 확인한다. 없으면 배포가 `pull_failed` 로 멈춘다:

```bash
docker buildx imagetools inspect ghcr.io/mgh3326/auto_trader:main
```

포트 충돌 확인. 아래 출력은 **비어 있어야** 한다(임시 포트 8100·8865 와 색 포트 네 쌍). `8000`·`8765` 는 컷오버 전에는 기존 컨테이너가 잡고 있는 것이 정상이다 **[live check]**:

```bash
ss -ltn | grep -E ':(8100|8865|18000|28000|18765|28765)[[:space:]]'
```

cloudflared-pi 터널이 가리키는 대상 주소를 터널 설정에서 확인한다 **[live check]**. `127.0.0.1:8000`(at-api)과 tailnet `100.90.137.33:8765`(at-mcp)가 아니면 컷오버를 시작하지 말고 설계 세션에 보고한다 — HAProxy 가 이 두 주소를 이어받는 것이 컷오버의 전제다.

설정 설치와 확인(컷오버 설정으로 시작한다):

```bash
install -d -m 0755 /etc/shiftgate /var/lib/shiftgate
install -m 0644 deploy/shiftgate/pi/services.cutover.yaml /etc/shiftgate/services.yaml
shiftgate render --dry-run
shiftgate render
```

`render --dry-run` 은 설정 검증(금지 `run_args`, `allowed_binds`, 색 포트 충돌, 와일드카드)을 통과해야 한다. `render` 는 `haproxy -c` 로 후보를 검증한 뒤 설치하고 리로드한다. 이 시점에는 모든 색이 disabled 다.

## 2. 컷오버 전 기록

컷오버 첫 shiftgate 배포는 **지금 실행 중인 digest 와 같은 digest** 로 한다(버전 변경과 배포 도구 변경을 섞지 않는다):

```bash
cat /root/at-run/deployed-digest
docker inspect --format '{{.Config.Image}}' at-api at-mcp at-upbit-ws
docker ps --filter name=at- --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

세 컨테이너(`at-api`, `at-mcp`, `at-upbit-ws`)의 이미지가 모두 같은 `ghcr.io/mgh3326/auto_trader@sha256:…` 인지 본다. 아직 `at-worker` 가 돌고 있으면 그 이미지는 이 비교에 넣지 않는다(은퇴 절차는 아래 「hk 5753 — at-worker 은퇴」). 다르면 보고하고 멈춘다. 같으면 그 digest 를 셸 변수에 담고 모양을 확인한다(다음 명령이 이 변수를 쓰므로 같은 셸에서 이어서 한다):

```bash
DIGEST="$(docker inspect --format '{{.Config.Image}}' at-api | sed 's/^[^@]*@//')"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] && echo "digest 확인: $DIGEST" || echo "중단: digest 형식이 아니다"
```

"중단" 이 나오면 여기서 멈추고 보고한다.

## 3. 컷오버 순서

기존 `at-api`·`at-mcp` 는 호스트 네트워크에서 `127.0.0.1:8000`, `100.90.137.33:8765` 를 직접 잡고 있어 HAProxy 가 같은 주소를 바인드할 수 없다. 그래서 두 단계로 옮긴다.

**다운타임**: 1단계는 기존 컨테이너가 계속 서비스하므로 **다운타임이 없다**. 2단계 전환만 끊긴다 — 기존 컨테이너 정지부터 HAProxy 가 주소를 이어받을 때까지 **수 초~1분 미만**: cloudflared 경유 at-api 요청은 그동안 502 를 받을 수 있고, tailnet MCP 클라이언트는 연결이 끊겼다가 재연결한다. 진행 중이던 MCP 도구 호출은 중단된다. 거래 시간(KR/US 정규장)과 진행 중인 승인 콜백·주문 도구 호출이 없는 시간에 한다. `at-upbit-ws` 는 컷오버에서 건드리지 않는다. `at-worker` 는 이 절차와 무관하다(hk 5753).

### 3.1 1단계 — 임시 포트에 shiftgate 첫 색 올리기 (다운타임 없음)

§1 의 `services.cutover.yaml` 이 설치된 상태에서 같은 digest 로 배포한다:

```bash
shiftgate deploy at-api --digest "$DIGEST"
shiftgate deploy at-mcp --digest "$DIGEST"
shiftgate status
```

임시 포트로 확인한다(기존 컨테이너는 그대로 서비스 중):

```bash
curl -fsS http://127.0.0.1:8100/healthz
curl -fsS http://100.90.137.33:8865/health
curl -s -o /dev/null -w '%{http_code}\n' http://100.90.137.33:8865/mcp
```

앞의 둘은 200, 마지막 `/mcp` 는 토큰 없이 **401** 이어야 한다. 하나라도 어긋나면 2단계로 가지 말고 `docker logs shiftgate-at-api-blue`, `docker logs shiftgate-at-mcp-blue` 를 본다. 1단계 되돌리기: `docker rm -f shiftgate-at-api-blue shiftgate-at-mcp-blue`, `rm -rf /var/lib/shiftgate/at-api /var/lib/shiftgate/at-mcp` 후 `shiftgate render`(상태 없음 → 두 색 disabled) — 기존 컨테이너는 한 번도 멈추지 않았다.

### 3.2 2단계 — 실제 바인드로 전환 (다운타임 구간)

```bash
install -m 0644 deploy/shiftgate/pi/services.yaml /etc/shiftgate/services.yaml
shiftgate render --dry-run
docker stop at-api at-mcp
shiftgate render
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://100.90.137.33:8765/health
curl -s -o /dev/null -w '%{http_code}\n' http://100.90.137.33:8765/mcp
shiftgate status
shiftgate check
```

`docker stop at-api at-mcp` 부터 `render` 가 성공할 때까지가 다운타임이다. 기존 컨테이너는 `--restart unless-stopped` 라 수동으로 멈추면 재부팅 뒤에도 멈춰 있고, 삭제하지 않으므로 롤백에 쓴다. `curl` 은 200·200·401 이어야 하고 `check` 는 두 서비스 모두 OK 여야 한다.

### 3.3 컷오버 롤백 (1단계 서비스 확인이 실패했거나 2단계 직후)

HAProxy 가 8000/8765 를 잡고 있으면 기존 컨테이너가 다시 뜰 수 없으므로 **바인드를 먼저 놓는다**:

```bash
install -m 0644 deploy/shiftgate/pi/services.cutover.yaml /etc/shiftgate/services.yaml
shiftgate render
docker start at-api at-mcp
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://100.90.137.33:8765/health
```

`render` 가 실패하면(상태 `pending` 등) `shiftgate recover at-api` 를 먼저 본다. 그래도 안 되면 마지막 수단으로 `systemctl stop haproxy` 후 `docker start at-api at-mcp` 로 컷오버 전 상태를 복원한다. 이 롤백은 shiftgate 이전 상태 그대로이며 shiftgate 컨테이너·상태 파일은 남는다(다음 시도 전에 §3.1 의 되돌리기로 지운다).

### 3.4 관찰 창과 정리

2단계 확인 뒤 기존 `at-api`·`at-mcp` 는 정지 상태로 **관찰 창(최소 1영업일)** 동안 둔다. 이 동안 롤백은 §3.3 이다. 창이 끝나고 이상이 없으면:

```bash
docker rm at-api at-mcp
```

**컨테이너를 지우기 전 조건**: 컷오버가 만든 shiftgate 색은 각 서비스에 하나뿐이라 아직 롤백 대상(이전 색)이 없다(f8a11c6 `rollback` 은 이전 색이 없으면 거부한다). 그래서 위 `docker rm` 은 **§4 의 일반 배포를 다른 digest 로 한 번 성공시킨 뒤**, `shiftgate status` 에서 `at-api`·`at-mcp` 두 서비스 모두 PREVIOUS 가 채워져 있고 롤백이 가능함을 확인한 다음에만 한다. 그 전에는 기존 컨테이너를 정지 상태로 남겨 §3.3 롤백을 유지한다.

컷오버로 `deployed-digest` 는 바뀌지 않는다(같은 digest). 첫 `deploy.sh` 가 처음 갱신한다.

## 4. 일상 배포

게이트가 그 태그를 불변 digest 로 고정하고 정확한 SHA 의 전체 스위트(`lint`, `taskiq-smoke`, `test (3.13, 1..4)`) 초록을 확인한다. 초록이 아니면 exit 78 로 끝나고 **어떤 컨테이너도 건드리지 않는다**:

```bash
/root/at-run/shiftgate-pi/deploy.sh main
```

순서: 호스트 잠금 → 게이트 → `at-api` → `at-mcp` → 싱글턴(`at-upbit-ws`) → 세 컴포넌트 digest 재확인 → `deployed-digest` 갱신(모두 확인된 뒤 한 번, 모든 파일 작업을 확인). 다른 배포·롤백이 돌고 있으면 exit 75 로 끝난다(잠금은 `/root/at-run/shiftgate-deploy.lock`, 죽은 소유자의 잠금은 자동으로 치운다). 대기 시간은 `at-api` 드레인 최대 120초, `at-mcp` 드레인 최대 300초(서비스 잠금을 잡고 CLI 를 막는다)다.

단계가 실패하면 거기서 멈추고 `shiftgate status` 와 컨테이너 표를 출력하며 `deployed-digest` 는 그대로다. **같은 명령을 다시 실행하면 이어진다**: 이미 그 digest 인 서비스·싱글턴은 건너뛰므로 보관된 이전 색 컨테이너와 `deployed-digest.previous` 가 보존되고(shiftgate 는 같은 digest 라도 비활성 색을 지우고 다시 만들기 때문에 건너뛰지 않으면 롤백 대상을 잃는다), 모두 확인된 뒤에만 마커가 갱신된다. 이미 전부 그 digest 이고 마커도 같으면 아무것도 하지 않는다. exit 3 이면 `shiftgate recover <service>` 를 먼저 한다(§5). `--allow-unverified REASON` 은 CI 증거만 면제하는 기록되는 우회이며 이미지 digest 고정은 그대로다:

```bash
/root/at-run/shiftgate-pi/deploy.sh --allow-unverified "<이유>" main
```

`deploy.sh` 의 인자는 **태그**다(`deploy_ci_gate.py --image` 가 태그만 받는다). 싱글턴만 실패했을 때도 같은 `deploy.sh <tag>` 를 다시 실행한다(이미 끝난 서비스는 건너뛰고 싱글턴만 교체하며 마커를 갱신한다). `swap-singletons.sh` 를 직접 부르지 않는다.

### 4.1 자동 배포 (follow, hk 6037)

AT `main` 커밋이 pi 에 스스로 배포된다. handoffkeep 과 같은 모양이다(hk:doc `decision/2026-11-08/hk-push-rollout-shiftgate`). **pi 로 들어오는 경로는 없다** — GitHub Actions 는 pi 에 접속하지 않고, pi 가 레지스트리를 읽는다.

```
main push → Test(전체 스위트) → deploy.yml 이 ghcr.io/mgh3326/auto_trader:sha-<7> 빌드
          → promote 잡(promote-deploy.yml)이 `deploy` 태그를 같은 digest 로 옮김(재빌드 없음)
          → pi 의 at-follow.timer(1분) → follow.sh → deploy.sh sha-<7>

          (별도) Dagu 일일 잡 at-pg-backup → systemctl start at-pg-backup.service
```

| 불변식 | 강제 위치 | 확인 |
|---|---|---|
| main 의 조상이고 전체 스위트가 초록인 SHA 만 설치된다 | promote 잡 + follow.sh(독립 재확인) | `tests/scripts/test_pi_follow.py`, `tests/scripts/test_deploy_ci_gate_anonymous.py` |
| deploy_approval 경로·새 마이그레이션·환경/비밀 변경·범위를 알 수 없는 배포는 자동으로 설치되지 않는다 | promote 잡 (`scripts/ci/promote_deploy.py`) | `tests/ci/test_promote_deploy.py` (mergewake 픽스처 87행 포함) |
| 태그를 손으로 옮겨도 검증 안 된 이미지는 설치되지 않는다 | follow.sh 가 태그 digest 의 SHA 를 스스로 다시 확인 | 실제 게이트 + 가짜 레지스트리/GitHub 로 end to end |
| 같은 digest 두 번은 무동작, 실패한 digest 는 매 틱 재시도하지 않는다 | follow.sh (`deployed-digest` 비교, `follow-hold`) | `test_pi_follow.py` |
| pi 로 들어오는 경로가 없다 | 설계(Actions 에 ssh·터널·배포 키 없음) | `tests/ci/test_promote_deploy_workflow.py` |

**승격(promote)의 판정** — mergewake 롤아웃의 규칙과 같다(`internal/pipeline/deploy_approval.go`, `docs/deploy-approval.md`). 후보 SHA 가 main 의 조상이고 전체 스위트 6개 job 이 초록이며 `sha-<7>` 이미지의 OCI revision 이 정확히 그 SHA 일 때만 다음으로 간다. 그 뒤 `deploy` 태그가 지금 가리키는 SHA(배포 중)와 후보 사이의 변경 범위를 `.mergewake/policy.yaml` 의 `deploy_approval` 로 판정한다. 정책은 **배포 중 커밋의 것**을 읽는다(릴리스가 자기 게이트를 풀 수 없다). 판정 도구(`promote_deploy.py`, `deploy_ci_gate.py`)도 후보의 트리가 아니라 **신뢰 ref(워크플로가 체크아웃한 main HEAD, `github.sha`)** 에서 실행되고 후보는 `--sha` 인자로만 들어간다(전체 이력을 받아 후보·배포 중 커밋이 모두 있다). 보류 조건: 경로(이름 변경은 두 이름 모두)가 규칙에 걸림 · 정책 파일 자체·환경/비밀로 보이는 경로 변경 · 레포 규칙으로 `alembic/versions/**`·`alembic/env.py` 변경(새 마이그레이션은 정책 문구와 무관하게 항상 보류) · 레포 규칙으로 판정·설치 도구 자체(`scripts/ci/promote_deploy.py`·`scripts/deploy_ci_gate.py`·`.github/workflows/promote-deploy.yml`·`.github/workflows/deploy.yml`·`deploy/shiftgate/pi/**`) 변경도 항상 보류 · 배포 중 커밋을 알 수 없음(`deploy` 태그가 없음 = 첫 승격 포함) · 이력이 갈라졌거나 체크아웃에 없음 · 비교가 300개 파일에 닿음 · 배포 중 커밋의 정책을 읽을 수 없음. 보류는 잡을 빨갛게 만들지 않는다: `deploy` 태그를 **옮기지 않고**(pi 는 이전 버전 유지) 잡 요약에 사유·걸린 경로·재승격 명령을 남기고 Discord(`DISCORD_WEBHOOK_URL`, 설정된 경우)로 알린다.

**보류된 범위의 승인** — 보류는 사람 결정이 풀며 Actions 는 hk 에 접속하지 않으므로 절차는 둘로 나뉜다:

1. hk 에 그 범위를 승인하는 결정을 남긴다(후보 SHA·걸린 경로·사유를 질문에 그대로 쓴다. 사람 승인 전용). 잡 요약이 그 문구를 준다.
2. 승인된 뒤 같은 SHA 를 다시 승격한다. `approval_ref` 에 hk 결정 문서 키를 넣는다:

```bash
gh workflow run promote-deploy.yml --ref main -f promote_sha="<40자 SHA>" -f approval_ref="<hk 결정 문서 키>"
```

승인은 **범위 보류만** 면제한다. CI·main 조상·이미지 확인은 다시 한다. `approval_ref` 가 있는 실행은 GitHub Environment `at-deploy-approval` 에 묶인 `promote-approved` 잡에서만 돈다 — 그 Environment 에 **required reviewers(사람)를 설정해야** 승인이 GitHub 에서 강제된다(1회성 설정, 설계 세션/운영자가 저장소 설정에서 만든다. 만들지 않으면 보호 없는 Environment 가 자동 생성되므로 승인 승격을 쓰기 전에 반드시 확인한다). 스크립트는 이 잡 밖의 `approval_ref` 를 거부한다. `promote-approved` 는 자동 promote 와 **다른 concurrency 그룹**(후보 SHA 별)이라 다음 main push 의 promote 가 대기 중인 승인 실행을 취소하지 못한다. 리뷰어 대기 중인 실행은 승인하거나 거절해 끝낸다. 두 잡이 동시에 돌 수 있으므로 스크립트는 태그를 옮기기 직전에 `deploy` 태그를 다시 읽어 검토 때와 달라졌으면 옮기지 않고 실패한다(태그를 되감지 않는다 — 다시 실행하면 된다). `approval_ref` 문자열 자체는 요약에 기록될 뿐 Actions 가 hk 에서 검증하지는 못한다. 이미 배포 중 커밋의 조상인 SHA 를 승인 승격하면 실패로 끝난다(승인이 적용되지 않았다는 뜻). hk 쪽 자동 재승격(답이 A 면 dispatch)은 후속 작업이다. 마이그레이션은 이 경로가 적용하지 않는다 — `deploy.sh` 에 마이그레이션 단계가 없다. 새 스키마를 기대하는 이미지가 설치되기 전에 운영자가 pi 의 기존 절차(백업·`alembic upgrade`)로 **먼저** 적용한 뒤 승인 승격을 한다.

**첫 승격(부트스트랩)** — `deploy` 태그가 아직 없으므로 승격은 보류된다. 지금 pi 에 배포된 SHA(`deployed-digest` 의 이미지 SHA)를 위 명령으로 승인 승격하면 태그가 그 digest 를 가리키고 follow.sh 는 아무것도 하지 않는다(같은 digest).

**1회성 설치와 켜기** — 설계 세션이 pi 에서 root 로 한다. 먼저 전제를 확인한다 **[live check]**: GHCR 읽기 로그인(§1), Python 3.11 이상. DB 백업은 Dagu 일일 잡 `at-pg-backup` 이 맡으며 follow 는 백업을 기다리지 않는다:

```bash
install -m 0755 deploy/shiftgate/pi/follow.sh deploy/shiftgate/pi/deploy.sh deploy/shiftgate/pi/host-lock.sh deploy/shiftgate/pi/swap-singletons.sh scripts/deploy_ci_gate.py /root/at-run/shiftgate-pi/
install -m 0644 deploy/shiftgate/pi/at-follow.service deploy/shiftgate/pi/at-follow.timer /etc/systemd/system/
systemctl daemon-reload
```

타이머를 켜기 전에 한 번 직접 돌려 본다. 부트스트랩이 끝나 있으면 아무 출력 없이 exit 0 이어야 한다(`deploy` 태그 digest == `deployed-digest`):

```bash
/root/at-run/shiftgate-pi/follow.sh
/root/at-run/shiftgate-pi/follow.sh --status
```

켜고 확인한다:

```bash
systemctl enable --now at-follow.timer
systemctl list-timers at-follow.timer
journalctl -u at-follow.service -n 50 --no-pager
```

끄기(자동 배포 중단, 수동 `deploy.sh` 는 그대로 쓸 수 있다):

```bash
systemctl disable --now at-follow.timer
```

**결과 읽기** — `/root/at-run/follow-status.json`(마지막 결과: `deployed`·`refused`·`failed`·`held`), `/root/at-run/follow.log`(`deploy.sh` 출력 포함), journal(`at-follow.service`). `AT_FOLLOW_PANEWIRE_LANE` 을 유닛 환경에 주면 `panewire` 가 있을 때 `info` 로 알린다(알림이 실패해도 배포는 영향 없다).

**hold** — 검증이 거부했거나 설치가 실패한 digest 는 `/root/at-run/follow-hold` 에 기록되어 다음 틱부터 건너뛴다(새 digest 가 오면 그 digest 를 다시 판정한다). GitHub 읽기 불가(게이트 자신의 문구 `GitHub evidence unavailable`·`unverified (` 만)는 retry-after(10분) 뒤 다시 시도하되 **digest 마다 최대 3번**(`AT_FOLLOW_MAX_TRANSIENT_RETRIES`)이고, 그 뒤에는 영구 hold 로 바뀐다(횟수는 `follow-transient`). follow.sh 가 방금 검증한 digest 는 `AT_FOLLOW_VERIFIED_DIGEST` 로 `deploy.sh` 에 넘겨 같은 틱에 GitHub 를 두 번 묻지 않는다(`deploy.sh` 는 `sha-<7>` 태그가 그 digest 인지만 레지스트리에서 확인하고, 변수가 없거나 어긋나면 전체 게이트를 돈다 — 수동 실행은 그대로다). 원인을 확인한 뒤 같은 digest 를 다시 시도하려면:

```bash
/root/at-run/shiftgate-pi/follow.sh --clear-hold
```

GitHub 인증 없는 읽기는 시간당 60회 한도다. follow.sh 는 digest 가 바뀐 틱에만 부르고(한 번에 약 5회, `deploy.sh` 의 게이트 포함) 같은 digest 는 hold 로 묶인다.

**롤백(자동 배포 중)** — 롤백한 digest 를 follow 가 다시 올리지 않게 **먼저 hold 하고** 롤백한다:

```bash
/root/at-run/shiftgate-pi/follow.sh --hold "<롤백 이유>"
/root/at-run/shiftgate-pi/deploy.sh --rollback
/root/at-run/shiftgate-pi/follow.sh --status
```

그다음 고친 릴리스가 승격되면(새 digest) follow 가 자동으로 올린다. 소스 revert 와 DB 롤백은 별개이며 §5 의 롤백 경계(hk 5753)는 그대로 적용된다.

**한계(알고 쓴다)**: `deploy` 태그를 쓸 수 있는 쪽(Actions 의 `GITHUB_TOKEN`, GHCR 패키지 쓰기 권한자)은 신뢰 경계다. follow.sh 의 재확인은 "main 의 조상 + 전체 스위트 초록"까지만 막는다 — 승인 보류가 걸렸어야 할 범위의 이미지를 손으로 태그에 올리면 설치된다. 그래서 패키지 쓰기 권한을 제한하고 태그를 손으로 옮기지 않는다. 마이그레이션이 든 릴리스를 승인 없이 태그에 올리지 않는다.

### 4.2 성공 뒤 오래된 이미지 정리 (hk 6437)

전진 배포가 `deployed-digest` 를 쓴 **뒤에만** `deploy.sh` 가 오래된 `ghcr.io/mgh3326/auto_trader` 이미지를 지운다. 수동 실행과 `follow.sh` 가 부르는 배포가 같다. 롤백, 실패한 배포, 이미 배포된 상태의 재실행은 아무것도 지우지 않는다.

- 보존: 현재 digest, `deployed-digest.previous`(롤백 대상), 실행 중·정지·생성만 된 컨테이너가 쓰는 모든 이미지.
- 지우지 않는 것: 다른 저장소의 참조가 섞인 이미지, 접두만 같은 다른 저장소, 참조 없는 이미지. 볼륨·컨테이너·네트워크 정리와 `docker system prune` 은 하지 않는다.
- 지우는 방법은 `docker image rm` 뿐이며 강제 옵션을 쓰지 않는다. 컨테이너가 쓰는 이미지는 docker 가 거부한다.
- 기록을 하나라도 읽지 못하거나 docker 조회가 실패하면 아무것도 지우지 않고 stderr 에 `WARNING` 을 남긴다. 정리가 실패해도 배포 결과(종료 코드 0)는 바뀌지 않는다.
- 출력: 이미지마다 한 줄, `image prune: removed <id> (<참조>)` 또는 `image prune: kept <id> (<사유>)`. 다른 줄은 내지 않는다. 그래서 NCP 스크립트와 달리 확보한 크기 합계는 출력하지 않는다.
- 끄기: `AT_IMAGE_PRUNE_ENABLED=0`(기본 `1`). `0` 과 `1` 이 아닌 값은 경고 후 건너뛴다.
- 규칙의 정본은 `docs/contracts/task-934-image-prune.md`(NCP 쪽)이고, pi 쪽은 같은 함수를 `deploy/shiftgate/pi/deploy.sh` 에 복사했다. 설치는 머지 뒤 설계 세션이 `deploy.sh` 와 함께 한다.

## 5. 롤백과 복구

모든 롤백은 `deploy.sh --rollback` 하나로 한다(호스트 잠금을 잡고, 게이트는 보지 않는 긴급 경로). 기준은 `deployed-digest` — **마지막으로 세 컴포넌트(`at-api`, `at-mcp`, `at-upbit-ws`)가 모두 끝난 릴리스**다:

- 어떤 컴포넌트가 그 digest 와 다르면(전진 배포가 중간에 실패한 경우) **다른 컴포넌트만** 그 digest 로 되돌린다. 보관된 이전 색이 그 digest 이면 pull 없이 `shiftgate rollback`, 아니면 그 digest 로 `shiftgate deploy`. 이미 맞는 컴포넌트와 `deployed-digest` 는 건드리지 않는다.
- 세 컴포넌트가 모두 그 digest 이면(끝난 릴리스를 되돌릴 때) `deployed-digest.previous` 로 완결 롤백하고, 세 컴포넌트를 확인한 **뒤에** 마커를 맞바꾼다(`previous` 가 없으면 exit 78).

```bash
/root/at-run/shiftgate-pi/deploy.sh --rollback
```

실행 전후로 상태를 본다(§6). 드레인 중이면 서비스 잠금 때문에 `shiftgate` 가 exit 75 를 내므로 드레인이 끝난 뒤 다시 한다. 배포 도중 exit 3 이 나왔으면 다른 일을 하기 전에 해당 서비스를 복구한 뒤 위 명령이나 `deploy.sh <tag>` 를 다시 실행한다:

```bash
shiftgate recover at-api
shiftgate recover at-mcp
```

싱글턴은 `swap-singletons.sh` 가 이전 컨테이너를 `<이름>-prev` 로 보관한다(한 세대). 실패한 교체는 스크립트가 알아서 복원한다. 교체가 옛 컨테이너를 보관한 직후 끊겨 `at-upbit-ws` 가 없고 `-prev` 만 있으면 스크립트가 거부하며 복구 명령을 출력한다 — 그 명령(`docker rename <이름>-prev <이름> && docker start <이름>`)을 실행한 뒤 다시 한다. 손으로 위의 롤백 단계를 흉내 내지 않는다(어느 세대가 최신인지 잘못 고르면 마커와 어긋난다).

## 6. 확인 명령

모두 읽기 전용이다(`check --heal` 은 컨테이너를 재시작하므로 이 런북에서 쓰지 않는다):

```bash
shiftgate status
shiftgate check
docker ps --filter name=at- --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
cat /root/at-run/deployed-digest
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://100.90.137.33:8765/health
curl -s -o /dev/null -w '%{http_code}\n' http://100.90.137.33:8765/mcp
systemctl status haproxy --no-pager
docker logs --tail 50 at-upbit-ws
```

`check` 는 서비스마다 활성 색 컨테이너, 헬스, HAProxy ready/UP 를 본다. `/mcp` 는 토큰 없이 401 이어야 한다(200 이면 인증이 빠진 것이므로 즉시 보고).

## hk 5753 — at-worker 은퇴

TaskIQ worker 는 은퇴했다(운영자 결정 `decision/2026-11-20/at-direction-daily-plan-nonprice-go` 항목 7). 이 디렉터리의 `deploy.sh`·`swap-singletons.sh` 는 더 이상 `at-worker` 를 시작하거나 교체하지 않는다(정적 테스트가 실행 줄에서 그 이름을 막는다).

### 배포 전 전제 (순서대로, 하나라도 건너뛰면 배포하지 않는다)

이 릴리스의 API 는 durable Telegram ingress 를 **503 으로 거부**하고(`TELEGRAM_CALLBACK_DURABLE_DRAIN_SUPPORTED=False`), 워커가 없으므로 이미 ACK 된 행을 비울 수 없다. 그래서 `at-worker` 를 멈추기 전에 W5 를 먼저 빼야 한다. 근거는 `telegram-callback-durable-inbox.md` §8 의 롤백 순서다.

1. 공유 env 에서 `ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED=false` 로 두고 `at-api` 를 재시작한다. 이후 콜백은 인라인 경로로 간다. 이 단계에서 env 가 `true` 로 남아 있으면 이 릴리스 배포 뒤 승인 버튼이 전부 503 이 된다.
2. 워커와 recovery 게이트가 켜진 채로 활성 행을 비운다. 모든 행이 `succeeded`·`discarded`·`dead_letter` 여야 한다. `pending`·`processing`·`retry_wait` 가 남아 있으면 **응답되지 않은 운영자 클릭**이므로 멈추고 보고한다.

   ```sql
   SELECT state, count(*) FROM review.telegram_callback_inbox GROUP BY state;
   ```
3. 워커와 recovery 게이트를 끈다(`ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED=false`, `ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED=false`).
4. 그 다음 이 릴리스를 배포한다(`deploy.sh <tag>`).

### 배포 뒤 정지

배포 뒤에는 **설계 세션이 한 번** 실행 중인 `at-worker` 컨테이너를 멈춘다. 위 3번이 끝난 뒤에만 한다:

```bash
docker stop -t 60 at-worker
docker ps -a --filter name=at-worker --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

정지만 하고 `docker rm` 은 하지 않는다. `--restart unless-stopped` 컨테이너는 수동 정지 뒤 데몬 재시작으로 되살아나지 않으므로 별도 설정이 필요 없다. 지우는 것은 후속 정리다. `deployed-digest` 에는 `at-worker` 가 들어가지 않으므로 롤백 대상도 없다.

W5 Telegram durable inbox 를 다시 켜려면 TaskIQ worker 복원이 먼저다. 복원과 함께 `TELEGRAM_CALLBACK_DURABLE_DRAIN_SUPPORTED` 를 검토된 변경으로 `True` 로 바꿔야 한다(`docs/runbooks/telegram-callback-durable-inbox.md` §4).

### 롤백 경계 (이 릴리스 이전 이미지로 되돌릴 때)

`deploy.sh --rollback` 은 `deployed-digest.previous` 나 마지막 끝난 digest 로 `at-api`·`at-mcp` 를 되돌린다. 그 이미지에는 TaskIQ producer 가 남아 있다(ops task-kick, 뉴스 판정 enqueue, W5 기본 kick). 워커가 멈춰 있는 상태에서 되살리면 아무도 받지 않는 메시지가 생긴다. 따라서 **롤백 전에 다음이 모두 참이어야 한다**:

- `ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED=false`, 그리고 W5 worker·recovery 게이트가 꺼져 있다. 워커를 되살리지 않는 한 켜지 않는다.
- `OPS_TASK_KICK_TOKEN` 이 비어 있다. 구 코드는 빈 토큰이면 `/trading/api/ops/tasks/*` 를 403 으로 닫는다. 토큰을 넣지 않는다.
- `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED=false`. 구 코드는 이 값이 켜지면 워커 없이 enqueue 한다.

위 조건을 확인할 수 없으면 롤백하지 않고 보고한다. `--rollback` 은 이 조건을 검사하지 않는다(긴급 경로).

## 7. 설계 선택과 한계

## 7. 설계 선택과 한계

- 색 포트는 루프백이다. at-mcp 앱은 `MCP_HOST=127.0.0.1`, `MCP_PORT={port}` 로 뜨고 tailnet 노출은 HAProxy 바인드(`100.90.137.33:8765`)가 맡는다. 이전 at-swap 의 `MCP_HOST=100.90.137.33` `MCP_PORT=8765` 와 `AUTO_TRADER_COLOR=pi` / `mcp-pi.json` 하트비트가 달라지는 유일한 곳이다(하트비트는 `mcp-blue.json`/`mcp-green.json`) **[live check: pi 에서 `mcp-pi.json` 을 읽는 소비자가 있는지]**.
- MCP 스트리밍: HAProxy `timeout client/server/tunnel 1h`. 응답을 버퍼링하지 않으므로 SSE 스트림이 그대로 흐른다. 헬스는 인증 없는 `GET /health`(이벤트 루프 생존)이며 `/mcp` 는 점검하지 않는다.
- 로그 드라이버는 `daemon`(현재 at-swap 과 같은 데몬 기본값).
- shiftgate 드레인은 CLI 를 `drain_timeout` 까지 막고 취소하지 않는다(shiftgate docs/design.md). 그래서 `at-mcp` 는 1시간이 아니라 300초다.
- `deploy.sh` 는 DB 백업·마이그레이션을 하지 않는다(pi 의 기존 절차 그대로 운영자 몫). 자동 배포(`follow.sh`)는 설치 전에 DB 백업을 돌리지 않는다(백업은 Dagu 일일 잡 `at-pg-backup`). 마이그레이션이 든 릴리스는 승격 단계에서 항상 보류된다(§4.1).

## 8. Exact-SHA full-suite gate와 이미지 태그 정책 (ROB-1456)

> 이 절은 삭제된 NCP pull-deploy 런북(`ncp-pull-deploy.md`, hk 6456 은퇴)의 현재도 유효한 내용을 옮긴 것이다. 규칙 문장은 바꾸지 않았고 경로만 pi 로 바꿨다.

**Exact-SHA 게이트.** 모든 전진 배포(`--dry-run` 포함)는 첫 이미지 pull·파일 쓰기·컨테이너 변경 전에 GitHub 를 확인한다. `scripts/deploy_ci_gate.py` 가 `docker buildx imagetools inspect` 로 원격 태그를 읽고 레지스트리 digest 를 고정한 뒤, 그 불변 이미지 플랫폼 config 의 `org.opencontainers.image.revision` 을 읽는다. 실행 가능한 모든 플랫폼이 같은 40자 전체 커밋 SHA 를 가리켜야 한다. 현재 `main` 끝, 짧은 태그 접미사, 로컬 캐시 이미지의 revision 으로 대체하지 않는다.

그 정확한 SHA 에 대해 가장 최근의 `Test` workflow push/dispatch run 과 그 최신 attempt 에 여섯 개 job(`lint`, `taskiq-smoke`, `test (3.13, 1)`~`test (3.13, 4)`)이 모두 완료·성공이어야 한다(`.github/workflows/test.yml`). ROB-1454 의 빠른 `PR gate`는 이 증거를 대신하지 못한다(`PR gate`, `ci-required`, PR run, skipped/neutral shard 는 받지 않는다). 결과를 run·attempt·커밋을 넘나들어 합치지 않는다. 없음·빨강·진행 중·취소·읽기 불가인 항목은 이름을 출력하고 **exit 78** 로 거부한다. API·인증 실패도 닫힌 쪽으로 실패한다. 암묵적 대기·재시도는 없다. CI 가 끝난 뒤 다시 실행한다. 수동 전체 스위트는 `gh workflow run test.yml --ref <branch>`(ROB-1454)로 돌리고 `head_sha` 가 의도한 커밋인지 확인한다.

성공하면 스크립트는 **이미 검증한 digest** 를 pull·승격한다(태그를 다시 읽지 않는다). 검사와 pull 사이에 태그가 움직여도 배포되는 커밋은 바뀌지 않는다. 자동 실패 복구와 명시적 `--rollback` 은 기존 불변 대상을 쓰는 **검사 없는 비상 경로**로 남는다.

`deploy-ok`(hk 3445)는 증거일 뿐이다. main push `Test` 뒤 배포 tier 가 그 digest 에 `deploy-ok` commit status 를 남기지만 `deploy_ci_gate.py` 는 이를 읽지 않는다. 전진 배포는 여전히 위 여섯 job 이 필요하고, 빨갛거나 없는 `deploy-ok` 는 아무것도 막지 않으며, 빨간 배포 tier 가 이미지를 게시 취소하지도 않는다. 상세: `docs/runbooks/ci-required-aggregator.md` §0a.

비상 전진 배포(운영자 결정; 사유는 사고 기록에 남기고 비밀은 넣지 않는다):

```bash
/root/at-run/shiftgate-pi/deploy.sh --allow-unverified 'INC-123: operator accepted missing full-suite evidence' main
```

`--allow-unverified REASON` 은 빈 값이 아닌 사유를 요구하고 `OVERRIDE`, 정확한 SHA, JSON 이스케이프된 사유, 없음·빨간 check 를 기록한다. CI 증거만 면제하며 이미지 identity 나 다른 배포 전제는 면제하지 않는다. digest·revision 이 없거나 플랫폼 SHA 가 섞이면 CI 우회와 무관하게 고쳐야 한다.

GHCR 애플리케이션 이미지 workflow(`.github/workflows/deploy.yml`)도 QEMU·Buildx 설정, registry 로그인, build/push 전에 같은 게이트를 쓴다. 자동 빌드는 이 레포 `main` push 의 완료된 `Test` run 을 따라가므로 CI 와 경쟁하지 않는다. checkout, build 인자, OCI revision, `sha-<short7>` 태그는 그 run 의 `head_sha` 를 쓰고 기본 브랜치 끝을 쓰지 않는다. `workflow_dispatch` 와 게시된 release 는 각자의 이벤트 SHA 를 검사한다. 수동 dispatch 는 같은 기록되는 우회 `allow_unverified` 를 제공한다. `workflow_run` 으로 기다리므로 docs-only main push 도 CI 뒤에 빌드된다(그 이벤트에 `paths-ignore` 가 없다). SHA 이미지 빌드에는 공유 concurrency group 이 없다. 적격한 모든 run 과 matrix 이미지는 별도 `promote-moving-tags` job 이 시작되기 전에 `sha-<short7>` 태그를 게시한다. 이동 태그 승격 job 만 비취소·이미지별 concurrency group 을 쓴다.

`main` 과 수동 dispatch 는 같은 승격 경로를 쓴다. 직렬화된 승격 job 안에서 Buildx·로그인 전에 정확한 SHA 게이트를 다시 실행하고, 게시 직전에 현재 `main` 끝을 읽는다. 일치하면 이미 게시된 SHA 이미지에서 **`main` 과 `latest` 를 함께** 승격한다. 과거 SHA 는 둘 다 바꾸지 않는다. 버전·릴리스·main 이 아닌 브랜치 태그는 이동 태그를 독립적으로 갱신하지 않는다(`metadata-action` 의 암묵 latest 생성은 끈다). 승격 job 이 밀리면 그 SHA 이미지는 여전히 배포 가능하고, main 의 수동 dispatch 가 짝 승격을 다시 요청할 수 있다.

**태그 정책.**
- `main` 과 `latest` 는 검증된 현재 `main` 이미지에서 함께 승격된다(자동 또는 main 수동 dispatch). 의도적으로 이동 가능하다.
- `sha-<short7>` 은 정확한 `main` 커밋 이미지를 가리키며 롤백이나 사고 고정의 선택지다.
- `production` 은 수동 릴리스 태그로 남는다. 이 workflow 는 main push 에서 올리지 않는다.
- GitHub Actions 이미지 workflow 는 `workflow_dispatch` 와 게시된 release 도 지원하며, 둘 다 같은 정확한 SHA 전체 스위트 증거를 요구한다.

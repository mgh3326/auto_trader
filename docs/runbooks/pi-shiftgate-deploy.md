# pi 배포를 shiftgate 로 (hk 5617)

pi(Debian 12, aarch64)의 AT 앱 컨테이너 네 개를 `shiftgate`(https://github.com/mgh3326/shiftgate, 고정 커밋 `f8a11c6`)와 어댑터 스크립트로 배포한다.

| 컨테이너 | 방식 | 노출 |
|---|---|---|
| `at-api` | shiftgate blue/green (`shiftgate-at-api-blue/green`, 색 포트 18000/28000, 호스트 네트워크) | HAProxy `127.0.0.1:8000` (cloudflared 터널 앞단) |
| `at-mcp` | shiftgate blue/green (`shiftgate-at-mcp-blue/green`, 색 포트 18765/28765, 호스트 네트워크) | HAProxy `100.90.137.33:8765` (tailnet 전용) |
| `at-worker`, `at-upbit-ws` | shiftgate 범위 밖 싱글턴(HTTP 아님 — shiftgate docs/design.md "gap"). `swap-singletons.sh` 가 이름 보관·정지·새 run·상태 확인·실패 시 복원 | 없음 |

레포 파일(운영자가 호스트에 복사한다. 레포는 아무것도 설치·실행·스케줄하지 않는다):

- `deploy/shiftgate/pi/services.yaml` — 최종 설정. `/etc/shiftgate/services.yaml`.
- `deploy/shiftgate/pi/services.cutover.yaml` — 컷오버 1단계 전용. `services.yaml` 과 `binds` 두 줄(임시 포트 8100/8865)만 다르다.
- `deploy/shiftgate/pi/deploy.sh <tag>` / `deploy.sh --rollback` — 일상 배포와 복구의 유일한 진입점.
- `deploy/shiftgate/pi/swap-singletons.sh <image@digest>` — `deploy.sh` 가 부르는 싱글턴 교체(직접 부르지 않는다).
- `deploy/shiftgate/pi/host-lock.sh` — 두 스크립트가 source 하는 호스트 배포 잠금.

규칙: `shiftgate follow` / `check` / `prune` 타이머를 등록하지 않는다(수동 배포만 — follow 는 `deploy_ci_gate.py` 의 정확한 SHA 전체 스위트 게이트를 우회한다). `MCP_PROFILE=default` 는 `services.yaml` 에 명시되어 있어야 한다(#1189). `deploy_ci_gate.py` 와 은퇴 컨테이너 검사 규칙(ROB-1436)은 완화하지 않는다. env 파일은 경로로만 참조하고 열거나 출력하지 않는다.

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
install -m 0755 deploy/shiftgate/pi/deploy.sh deploy/shiftgate/pi/swap-singletons.sh deploy/shiftgate/pi/host-lock.sh scripts/deploy_ci_gate.py /root/at-run/shiftgate-pi/
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
docker inspect --format '{{.Config.Image}}' at-api at-mcp at-worker at-upbit-ws
docker ps --filter name=at- --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

네 컨테이너의 이미지가 모두 같은 `ghcr.io/mgh3326/auto_trader@sha256:…` 인지 본다. 다르면 보고하고 멈춘다. 같으면 그 digest 를 셸 변수에 담고 모양을 확인한다(다음 명령이 이 변수를 쓰므로 같은 셸에서 이어서 한다):

```bash
DIGEST="$(docker inspect --format '{{.Config.Image}}' at-api | sed 's/^[^@]*@//')"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] && echo "digest 확인: $DIGEST" || echo "중단: digest 형식이 아니다"
```

"중단" 이 나오면 여기서 멈추고 보고한다.

## 3. 컷오버 순서

기존 `at-api`·`at-mcp` 는 호스트 네트워크에서 `127.0.0.1:8000`, `100.90.137.33:8765` 를 직접 잡고 있어 HAProxy 가 같은 주소를 바인드할 수 없다. 그래서 두 단계로 옮긴다.

**다운타임**: 1단계는 기존 컨테이너가 계속 서비스하므로 **다운타임이 없다**. 2단계 전환만 끊긴다 — 기존 컨테이너 정지부터 HAProxy 가 주소를 이어받을 때까지 **수 초~1분 미만**: cloudflared 경유 at-api 요청은 그동안 502 를 받을 수 있고, tailnet MCP 클라이언트는 연결이 끊겼다가 재연결한다. 진행 중이던 MCP 도구 호출은 중단된다. 거래 시간(KR/US 정규장)과 진행 중인 승인 콜백·주문 도구 호출이 없는 시간에 한다. `at-worker`·`at-upbit-ws` 는 컷오버에서 건드리지 않는다.

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

순서: 호스트 잠금 → 게이트 → `at-api` → `at-mcp` → 싱글턴 → 네 컴포넌트 digest 재확인 → `deployed-digest` 갱신(모두 확인된 뒤 한 번, 모든 파일 작업을 확인). 다른 배포·롤백이 돌고 있으면 exit 75 로 끝난다(잠금은 `/root/at-run/shiftgate-deploy.lock`, 죽은 소유자의 잠금은 자동으로 치운다). 대기 시간은 `at-api` 드레인 최대 120초, `at-mcp` 드레인 최대 300초(서비스 잠금을 잡고 CLI 를 막는다)다.

단계가 실패하면 거기서 멈추고 `shiftgate status` 와 컨테이너 표를 출력하며 `deployed-digest` 는 그대로다. **같은 명령을 다시 실행하면 이어진다**: 이미 그 digest 인 서비스·싱글턴은 건너뛰므로 보관된 이전 색 컨테이너와 `deployed-digest.previous` 가 보존되고(shiftgate 는 같은 digest 라도 비활성 색을 지우고 다시 만들기 때문에 건너뛰지 않으면 롤백 대상을 잃는다), 모두 확인된 뒤에만 마커가 갱신된다. 이미 전부 그 digest 이고 마커도 같으면 아무것도 하지 않는다. exit 3 이면 `shiftgate recover <service>` 를 먼저 한다(§5). `--allow-unverified REASON` 은 CI 증거만 면제하는 기록되는 우회이며 이미지 digest 고정은 그대로다:

```bash
/root/at-run/shiftgate-pi/deploy.sh --allow-unverified "<이유>" main
```

`deploy.sh` 의 인자는 **태그**다(`deploy_ci_gate.py --image` 가 태그만 받는다). 싱글턴만 실패했을 때도 같은 `deploy.sh <tag>` 를 다시 실행한다(이미 끝난 서비스는 건너뛰고 싱글턴만 교체하며 마커를 갱신한다). `swap-singletons.sh` 를 직접 부르지 않는다.

## 5. 롤백과 복구

모든 롤백은 `deploy.sh --rollback` 하나로 한다(호스트 잠금을 잡고, 게이트는 보지 않는 긴급 경로). 기준은 `deployed-digest` — **마지막으로 네 컴포넌트가 모두 끝난 릴리스**다:

- 어떤 컴포넌트가 그 digest 와 다르면(전진 배포가 중간에 실패한 경우) **다른 컴포넌트만** 그 digest 로 되돌린다. 보관된 이전 색이 그 digest 이면 pull 없이 `shiftgate rollback`, 아니면 그 digest 로 `shiftgate deploy`. 이미 맞는 컴포넌트와 `deployed-digest` 는 건드리지 않는다.
- 네 컴포넌트가 모두 그 digest 이면(끝난 릴리스를 되돌릴 때) `deployed-digest.previous` 로 완결 롤백하고, 네 컴포넌트를 확인한 **뒤에** 마커를 맞바꾼다(`previous` 가 없으면 exit 78).

```bash
/root/at-run/shiftgate-pi/deploy.sh --rollback
```

실행 전후로 상태를 본다(§6). 드레인 중이면 서비스 잠금 때문에 `shiftgate` 가 exit 75 를 내므로 드레인이 끝난 뒤 다시 한다. 배포 도중 exit 3 이 나왔으면 다른 일을 하기 전에 해당 서비스를 복구한 뒤 위 명령이나 `deploy.sh <tag>` 를 다시 실행한다:

```bash
shiftgate recover at-api
shiftgate recover at-mcp
```

싱글턴은 `swap-singletons.sh` 가 이전 컨테이너를 `<이름>-prev` 로 보관한다(한 세대). 실패한 교체는 스크립트가 알아서 복원한다. 교체가 옛 컨테이너를 보관한 직후 끊겨 `at-worker`(또는 `at-upbit-ws`)가 없고 `-prev` 만 있으면 스크립트가 거부하며 복구 명령을 출력한다 — 그 명령(`docker rename <이름>-prev <이름> && docker start <이름>`)을 실행한 뒤 다시 한다. 손으로 위의 롤백 단계를 흉내 내지 않는다(어느 세대가 최신인지 잘못 고르면 마커와 어긋난다).

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
docker logs --tail 50 at-worker
docker logs --tail 50 at-upbit-ws
```

`check` 는 서비스마다 활성 색 컨테이너, 헬스, HAProxy ready/UP 를 본다. `/mcp` 는 토큰 없이 401 이어야 한다(200 이면 인증이 빠진 것이므로 즉시 보고).

## 7. 설계 선택과 한계

- 색 포트는 루프백이다. at-mcp 앱은 `MCP_HOST=127.0.0.1`, `MCP_PORT={port}` 로 뜨고 tailnet 노출은 HAProxy 바인드(`100.90.137.33:8765`)가 맡는다. 이전 at-swap 의 `MCP_HOST=100.90.137.33` `MCP_PORT=8765` 와 `AUTO_TRADER_COLOR=pi` / `mcp-pi.json` 하트비트가 달라지는 유일한 곳이다(하트비트는 `mcp-blue.json`/`mcp-green.json`) **[live check: pi 에서 `mcp-pi.json` 을 읽는 소비자가 있는지]**.
- MCP 스트리밍: HAProxy `timeout client/server/tunnel 1h`. 응답을 버퍼링하지 않으므로 SSE 스트림이 그대로 흐른다. 헬스는 인증 없는 `GET /health`(이벤트 루프 생존)이며 `/mcp` 는 점검하지 않는다.
- 로그 드라이버는 `daemon`(현재 at-swap 과 같은 데몬 기본값).
- shiftgate 드레인은 CLI 를 `drain_timeout` 까지 막고 취소하지 않는다(shiftgate docs/design.md). 그래서 `at-mcp` 는 1시간이 아니라 300초다.
- `deploy.sh` 는 DB 백업·마이그레이션을 하지 않는다(pi 의 기존 절차 그대로 운영자 몫).

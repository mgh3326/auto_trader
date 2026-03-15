# n8n Workflow Automation

auto_trader의 워크플로우 오케스트레이션을 담당하는 n8n 인스턴스.

## 구조

```
n8n/
├── data/          # n8n 런타임 데이터 (.gitignore)
├── workflows/     # 워크플로우 JSON export (Git 추적)
└── README.md
```

## 역할

- **알림 파이프라인**: 체결/분석 이벤트 수신 → Discord/Telegram 라우팅
- **헬스체크**: MCP 서버, WebSocket 연결 상태 모니터링
- **리포트**: 일일/주간 포트폴리오 요약 자동 발송
- **외부 시그널**: 경제 캘린더, 뉴스 등 수집

## 인증

n8n 2.x는 owner 기반 User Management를 사용합니다.
첫 접속 시 owner 계정 생성 화면이 표시됩니다.

- **내부 전용**: `N8N_LISTEN_ADDRESS=127.0.0.1`로 localhost만 바인딩
- 외부 접근: SSH 터널 사용 (`ssh -L 5678:localhost:5678 user@host`)

## 헬스체크

`QUEUE_HEALTH_CHECK_ACTIVE=true`로 `/healthz` 엔드포인트가 활성화되어 있습니다.

```bash
curl -f http://127.0.0.1:5678/healthz
```

Docker healthcheck + `scripts/healthcheck.sh` 모두 이 엔드포인트를 사용합니다.

## 워크플로우 관리

n8n UI에서 워크플로우 수정 후 export:

```bash
# 전체 워크플로우를 개별 JSON으로 export
docker exec auto_trader_n8n_prod n8n export:workflow \
  --all --separate --output=/home/node/.n8n/workflows/

# 호스트의 n8n/data/workflows/ → n8n/workflows/로 복사
cp n8n/data/workflows/*.json n8n/workflows/

# Git 커밋
git add n8n/workflows/ && git commit -m "chore: export n8n workflows"
```

import (복원 시):
```bash
docker exec auto_trader_n8n_prod n8n import:workflow \
  --input=/home/node/.n8n/workflows/
```

## 런타임 계약

n8n은 낮부 전용으로 `127.0.0.1:5678`에 고정 바인딩됩니다.

## 배포 및 소유권 경계

**중요**: `n8n`은 `scripts/deploy.sh`와 별개로 관리됩니다.

- `deploy.sh`는 API stack (`docker-compose.prod.yml`)만 배포하고 검증합니다
- `n8n`은 별도 compose 파일로 수동/독립적으로 시작/중지/업데이트합니다
- `deploy.sh`는 `n8n`의 시작, 재시작, 또는 헬스체크를 수행하지 않습니다

## 배포

```bash
# 별도 compose 파일로 실행 (caddy와 동일 패턴)
docker compose -f docker-compose.n8n.yml up -d

# 로그 확인
docker compose -f docker-compose.n8n.yml logs -f

# 중지
docker compose -f docker-compose.n8n.yml down
```

`network_mode: host`이므로 auto_trader API/MCP와 localhost로 직접 통신 가능.

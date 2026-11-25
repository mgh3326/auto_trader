# Raspberry Pi Docker 배포 가이드 (Retired)

> **상태:** 폐기됨 / 사용 금지  
> **이유:** ROB-263에서 Raspberry Pi Docker production deploy 경로를 제거했습니다.  
> **현재 production 경로:** pi shiftgate 배포(`docs/runbooks/pi-shiftgate-deploy.md`)만 사용합니다.

## 현재 운영 원칙

- Auto Trader production은 pi Docker 서비스(shiftgate)가 단일 owner입니다.
- 특히 KIS websocket은 **pi single-owner**로만 운영해야 합니다.
- Raspberry Pi에서 `docker-compose.prod.yml`로 production stack을 다시 올리면 KIS websocket appkey/session 점유 충돌(`OPSP8996 ALREADY IN USE appkey`)을 재발시킬 수 있습니다.

## 사용해야 하는 배포 경로

```bash
# pi shiftgate deployment (operator, on the pi host)
deploy/shiftgate/pi/deploy.sh <tag>
```

수동 배포가 필요하면 [pi shiftgate runbook](docs/runbooks/pi-shiftgate-deploy.md)에 따라 pi 호스트에서 실행하세요. `scripts/deploy-native.sh`는 항상 exit 70으로 종료합니다.

## 과거 Raspberry Pi stack 정리 명령

아래 명령은 **새 배포용이 아니라 기존 Raspberry Pi host 정리용**입니다.

```bash
cd /home/mgh3326/auto_trader

docker compose --env-file .env.prod -f docker-compose.prod.yml down --remove-orphans
```

정리 후에는 다음을 확인하세요.

```bash
docker ps -a | grep '^auto_trader_' || true
ps aux | grep -E 'websocket_monitor|kis_websocket|upbit_websocket' | grep -v grep || true
```

## 참고

이 문서는 과거 운영 흔적을 남기기 위한 retired 문서입니다. 신규 Raspberry Pi Docker production 설정 절차를 추가하지 마세요.

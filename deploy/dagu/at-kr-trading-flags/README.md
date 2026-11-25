# at-kr-trading-flags (hk 6503) — Dagu 복사본, 등록되지 않음

`at-kr-trading-flags-intraday.yaml` 은 네이버 관리·거래정지·경보 목록을 10분마다
`kr_symbol_trading_flags` 에 반영하는 장중 보조 갱신이다. **저장소는 이 파일을 등록하지 않는다.**
등록과 스케줄 변경은 설계 세션이 한다.

일 1회 KIS 종목정보 기준값 갱신은 이 DAG 에 포함하지 않았다. 등록 전에 설계 세션이 다음 중 하나를 정한다.

- `uv run python -m scripts.sync_kr_trading_flags --source kis-master` 를 일 1회 실행
  (정규장 개장 전, 예: 08:00 KST). 명령은 같은 Docker 이미지 패턴을 따른다.
- 먼저 `--dry-run` 으로 KIS 종목정보 파일의 값 분포를 확인한다 (`[unverified]`: 시장경고 02/03, 관리 `3` 등).

검증 명령:

```bash
uv run python -m scripts.sync_kr_trading_flags --source naver-intraday --dry-run
uv run python -m scripts.sync_kr_trading_flags --source kis-master --dry-run
```

`--dry-run` 은 파싱과 개수만 낸다. 쓰기는 실패 시 아무것도 남기지 않는다(소스 실패가 먼저 종료).

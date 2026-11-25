# at-kr-trading-flags (hk 6503) — Dagu 복사본, 등록되지 않음

두 DAG 복사본이 있다. **저장소는 둘 다 등록하지 않는다.** 등록과 스케줄 변경은 설계 세션이 한다.

- `at-kr-trading-flags-daily.yaml` — 월–금 07:00 KST. KIS 종목정보 zip 기준값 (`--source kis-master`).
- `at-kr-trading-flags-intraday.yaml` — 월–금 09:00–15:50 KST, 10분 간격. 네이버 관리·거래정지·경보 목록 보조 (`--source naver-intraday`).

두 DAG 는 함께 등록해야 한다. 네이버 목록은 거래정지·경고 종목을 전부 담지 않으므로 장중 보조만으로는 해제 상태가 남는다.
저장소의 스크리너는 96시간보다 오래된 관측을 unknown 으로 보고 제외하지 않는다(`meta.trading_flags_stale`).

검증 명령 (수동, `--dry-run` 은 쓰지 않는다):

```bash
uv run python -m scripts.sync_kr_trading_flags --source kis-master --dry-run
uv run python -m scripts.sync_kr_trading_flags --source naver-intraday --dry-run
```

시장경고 매핑이 대부분 `unmapped` 이면 kis-master 쓰기를 거부한다(종료 코드 1, 아무것도 쓰지 않음).

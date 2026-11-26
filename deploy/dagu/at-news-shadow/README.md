# at-news-shadow

Benzinga·Finnhub·KIS 뉴스 그림자 수집 Dagu 복사본. 이 디렉터리는 등록되지 않는 복사본이며
저장소는 Dagu 에 복사하거나 등록하지 않는다.
- `at-news-shadow.yaml` — 평일 04:00–20:00 ET, 30분 간격으로
  `scripts.benzinga_news_shadow poll --symbols NVDA,AMD,QQQ,SMH,DELL,URBN --out-dir /out` 실행.
  게이트(`BENZINGA_NEWS_SHADOW_ENABLED=true`)는 `docker run -e` 로 컨테이너에 직접
  전달한다(DAG `env:` 는 호스트 셸에만 보여 컨테이너에 닿지 않는다).
  `BENZINGA_API_KEY` 키는 job env(`--env-file /root/at-secrets/.env.api`)에서
  오고 레포에 두지 않는다. 출력은 `-v /root/at-run/benzinga-shadow:/out` 마운트로
  호스트에 남는다(`--rm` 컨테이너 안 경로는 사라진다).
  호스트 디렉터리는 컨테이너의 appuser(uid 10001)가 쓸 수 있어야 하며, 등록 시점에
  운영자가 만든다.
- 등록은 hk 6395 머지 뒤 설계 세션이 한다(키·구매는 운영자 결정).
- Save+ 는 별도 미등록 복사본 `deploy/dagu/at-saveticker-shadow/` 에서만
  KST 매일 07:00–23:59, 3분 간격으로 실행한다. 해당 CLI 는
  `--skip-benzinga --skip-finnhub --skip-kis` 로 기존 소스 주기를 유지한다.
- Save+ 공개 앱 백엔드 `api.prod.fivelines.co.kr` 만 읽는다. 비공식이며 변경될 수
  있고 인증 경로, `www.saveticker.com`, SSE 는 사용하지 않는다. 실패는
  `poll_status` 오류 행과 종료 코드 1 로 남긴다. `attention` 행은 poll 마다 기록한다.

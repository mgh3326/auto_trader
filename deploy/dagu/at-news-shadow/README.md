# at-news-shadow

Benzinga 뉴스 그림자 수집 Dagu 복사본. 이 디렉터리는 등록되지 않는 복사본이며
저장소는 Dagu 에 복사하거나 등록하지 않는다.
- `at-news-shadow.yaml` — 미국 정규장(월–금 09:00–16:00 ET, 30분 간격)에
  `scripts.benzinga_news_shadow poll --symbols NVDA,AMD,QQQ,SMH --out-dir /out` 실행.
  게이트(`BENZINGA_NEWS_SHADOW_ENABLED=true`)는 `docker run -e` 로 컨테이너에 직접
  전달한다(DAG `env:` 는 호스트 셸에만 보여 컨테이너에 닿지 않는다).
  `BENZINGA_API_KEY` 키는 job env(`--env-file /root/at-secrets/.env.api`)에서
  오고 레포에 두지 않는다. 출력은 `-v /root/at-run/benzinga-shadow:/out` 마운트로
  호스트에 남는다(`--rm` 컨테이너 안 경로는 사라진다).
  호스트 디렉터리는 컨테이너의 appuser(uid 10001)가 쓸 수 있어야 하며, 등록 시점에
  운영자가 만든다.
- 등록은 hk 6395 머지 뒤 설계 세션이 한다(키·구매는 운영자 결정).

# at-news-shadow

Benzinga 뉴스 그림자 수집 Dagu 복사본. 이 디렉터리는 등록되지 않는 복사본이며
저장소는 Dagu 에 복사하거나 등록하지 않는다.

- `at-news-shadow.yaml` — 미국 정규장(월–금 09:00–16:00 ET, 30분 간격)에
  `scripts.benzinga_news_shadow poll --symbols NVDA,AMD,QQQ,SMH` 실행.
  `BENZINGA_API_KEY` 키는 job env(`--env-file /root/at-secrets/.env.api`)에서
  오고 레포에 두지 않는다. 출력은 repo 밖(`/root/at-run/benzinga-shadow`).
- 등록은 hk 6395 머지 뒤 설계 세션이 한다(키·구매는 운영자 결정).

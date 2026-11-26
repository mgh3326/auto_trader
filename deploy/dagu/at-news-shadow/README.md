# at-news-shadow

Benzinga·Finnhub·KIS·Save+ 뉴스 그림자 수집 Dagu 복사본. 이 디렉터리는 등록되지 않는 복사본이며
저장소는 Dagu 에 복사하거나 등록하지 않는다.
- `at-news-shadow.yaml` — KST 매일 07:00–23:59, 3분 간격으로
  `scripts.benzinga_news_shadow poll --symbols NVDA,AMD,QQQ,SMH,DELL,URBN --out-dir /out --saveticker --saveticker-pages 3` 실행.
  게이트(`BENZINGA_NEWS_SHADOW_ENABLED=true`)는 `docker run -e` 로 컨테이너에 직접
  전달한다(DAG `env:` 는 호스트 셸에만 보여 컨테이너에 닿지 않는다).
  `BENZINGA_API_KEY` 키는 job env(`--env-file /root/at-secrets/.env.api`)에서
  오고 레포에 두지 않는다. 출력은 `-v /root/at-run/benzinga-shadow:/out` 마운트로
  호스트에 남는다(`--rm` 컨테이너 안 경로는 사라진다).
  호스트 디렉터리는 컨테이너의 appuser(uid 10001)가 쓸 수 있어야 하며, 등록 시점에
  운영자가 만든다.
- 등록은 hk 6395 머지 뒤 설계 세션이 한다(키·구매는 운영자 결정).
- Save+ 는 공개 앱 백엔드 `api.prod.fivelines.co.kr` 만 읽으며 인증 경로,
  `www.saveticker.com`, SSE 는 사용하지 않는다. 백엔드는 비공식이며 변경될 수
  있다. 실패는 `poll_status` 오류 행과 종료 코드 1 로 남고 빈 결과로 숨기지 않는다.
- Save+ 뉴스 본문과 참여 수치는 각 poll JSONL 에 저장한다. 기사와 별도로
  `record_type=attention` 행을 남겨 ticker 별 조회·댓글·좋아요·투표 증감을
  나중에 계산할 수 있다. 페이지 수는 1–10 범위이며 이전 JSONL 에서 첫 기존 ID 를
  만나면 최신 구간 수집을 멈춘다.

# 비가격 이력 정리 (hk 5751)

운영자 결정 `decision/2026-11-20/at-direction-daily-plan-nonprice-go` 항목 5에 따라,
더 이상 쌓이지 않는 뉴스 피드·경제지표 이벤트와 Toss 시가총액 이력을 정리한다.
감사 근거는 `consult/2026-11-20/at-nonprice-data-audit-fable52` §1.2, §1.4, §1.5, §4다.
백업은 2026-11-18 전체 덤프(Drive `fleet-backups/ncp-final-20261118`)다.

CLI는 `scripts/prune_nonprice_history.py` 하나다. 기본은 미리보기이며, 개수만 출력한다
(URL·제목·종목·payload는 출력하지 않는다). 스케줄러 연결은 없다. 설계 세션이 배포 후
pi에서 수동으로 실행하고, 그 뒤 VACUUM을 한다.

## 대상

| 범주 | 대상 | 같이 지워지는 행 |
|---|---|---|
| `news` | `news_articles.feed_source` 20개: `rss_yahoo_finance_topstories`, `browser_naver_mainnews`, `rss_cnbc_us_markets`, `browser_naver_research_company`, `rss_marketwatch_topstories`, `rss_cointelegraph`, `rss_coindesk`, `browser_naver_research`, `browser_naver_research_industry`, `rss_decrypt`, `browser_naver_research_invest`, `rss_bitcoin_magazine`, `browser_naver_research_economy`, `browser_investing_news`, `browser_cnbc_news`, `rss_cnbc_earnings`, `rss_fed_press`, `rss_cnbc_finance`, `http_tvscreener_news_kr`, 그리고 보류 중인 `http_naver_stock_aggregate` | `symbol_news_relevance`, `news_article_related_symbols`, `news_analysis_results` |
| `market_events` | `(tradingview, economic, global)`, `(forexfactory, economic, global)`, `(wisefn, earnings, kr)` | `market_event_values`, `market_event_ingestion_partitions` (같은 트리플) |
| `toss_valuation` | `market_valuation_snapshots` 중 `source='toss_openapi'`이며 같은 `(market, symbol)`의 더 최신 Toss 행이 있는 행 | 없음 |

남기는 행: 뉴스 `naver_item_news`, `finnhub_company_news`, `finnhub_general_news`,
`research_on_demand_naver`, `research_on_demand_finnhub`. 시장 이벤트 `dart`, `finnhub`.
밸류에이션 `naver_finance`, `tvscreener`(hk 5755에서 다룬다) 행과 Toss의 최신 행.

## 보호 기사

삭제 피드의 기사라도 보존 피드(`naver_item_news` 등)의 `symbol_news_relevance` 연결이 있으면 지우지 않는다. 미리보기의 `protected_by_retained_feed_link`가 그 개수다.

## 보류: `http_naver_stock_aggregate`

이 피드는 기본 실행에서 삭제하지 않는다. 두 가지 이유가 있다.

- `app/services/spike_attribution/spec.py`와 `materials.py`가 이 피드를 KR 정확 시각
  (`KR_EXACT`)의 유일한 적격 근거로 쓴다. 지우면 과거 spike 귀속의 근거가 사라진다.
- `scripts/backfill_news_related_symbols.py`가 이 피드를 대상으로 한다.

설계 세션이 결정하기 전에는 `--allow-held-feed http_naver_stock_aggregate`를 주지 않는다.
(hk 5751 ESC, 기본값 = 보류.)

## 실행 절차

1. 배포 뒤 pi에서 미리보기를 실행한다. 출력의 `by_feed`를 기록한다.

   ```bash
   uv run python -m scripts.prune_nonprice_history --database-url-env ATD_DB_URL
   ```

   `--database-url-env`는 PostgreSQL URL을 담은 환경 변수 이름이다. 값은 출력되지 않는다.
   그 밖의 환경 변수는 읽지 않는다.

2. `symbol_news_relevance`·`news_article_related_symbols` 건수가 0이 아닌 피드가 있으면,
   `get_news`(정규 읽기 `symbol_news_store.load_symbol_news`)가 그 연결 기사를 더 이상
   보여주지 않게 된다는 뜻이다. 이 건수를 근거로 설계 세션의 결정을 받는다.

3. 결정을 받은 뒤 범주별로 실행한다. 배치 크기는 기본 5000이다.

   ```bash
   uv run python -m scripts.prune_nonprice_history --database-url-env ATD_DB_URL \
     --commit --only news --batch-size 5000
   uv run python -m scripts.prune_nonprice_history --database-url-env ATD_DB_URL \
     --commit --only market_events
   uv run python -m scripts.prune_nonprice_history --database-url-env ATD_DB_URL \
     --commit --only toss_valuation
   ```

   각 배치는 독립된 트랜잭션이다. 중간에 멈추면 다시 실행하면 된다. 이미 지운 행은
   다시 선택되지 않는다. 두 번째 실행은 아무것도 지우지 않는다.

4. 끝나면 VACUUM을 한다(pi에서 운영자가 수행한다).

## 종료 코드와 실패 처리

- `0`: 미리보기 또는 커밋이 끝났다.
- `1`: 입력 오류, 데이터베이스 오류, 또는 배치 무결성 불일치다. 불일치는
  `batch deleted N of M selected rows`로 나타난다. 그 배치는 롤백되었다.
  원인을 확인한 뒤 다시 실행한다.

## 쓰기 경로 (Toss 밸류에이션)

`sync_toss_symbol_master`는 이제 커밋 때 같은 트랜잭션에서
`MarketValuationSnapshotsRepository.prune_superseded`를 호출한다. 이 호출은 써 넣은
심볼에 대해 같은 `source`의 더 오래된 행을 지운다. 다른 소스의 행은 건드리지 않는다.
따라서 Toss 행은 `(market, symbol)`마다 최신 한 개만 남는다.
`superseded_by_newer_row`(`app/services/market_valuation_snapshots/repository.py`)가
CLI와 writer가 공유하는 판정식이다.

## 테스트

- 로컬: `uv run pytest tests/scripts/test_prune_nonprice_history.py`(선택, 배치 경계,
  보류 피드, 미리보기 무쓰기, writer SQL)
- CI: `tests/scripts/test_prune_nonprice_history_db.py`(커밋이 정확한 행과 의존 행만
  지우는지, 한 행 배치, 두 번째 실행 무동작)

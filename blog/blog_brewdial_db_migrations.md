# 사이드 프로젝트 DB 세 번 이사기: CouchDB에서 Supabase, 그리고 OCI PostgreSQL까지

![BrewDial DB 3회 이사](images/brewdial_db_thumbnail.png)

## DB를 세 번 옮겼습니다. 셋 다 계획이 아니었습니다

첫 번째 DB를 고를 때 ADR(Architecture Decision Record)까지 써 가며 신중하게 결정했습니다. 그 DB는 두 달을 살았습니다. 두 번째 DB는 일주일을 살았습니다. 세 번째 DB로 옮기는 설계 문서를 쓰기 시작한 게, 두 번째 DB로 이사를 마친 바로 다음 날이었습니다.

이 글은 커피 사이드 프로젝트 하나가 CouchDB → Supabase → 자체 운영 PostgreSQL(Oracle Cloud)로 데이터를 두 번 통째로 옮긴 기록입니다. 각 이사는 그 시점에는 전혀 예정에 없었고, 매번 "이 선택이 틀려서"가 아니라 "요구사항이 바뀌어서" 일어났습니다. 사이드 프로젝트를 무료로 실배포하고 싶은 분이라면, 제가 세 정거장에서 각각 무엇을 얻고 무엇에 부딪혔는지가 참고가 될 겁니다.

프로젝트 이름은 <b>BrewDial</b>입니다. 매일 아침 V60 핸드드립을 내리면서 레시피(원두, 분쇄도, 물온도, 푸어 스케줄)를 기록하고, 추출 후 맛 피드백(신맛/쓴맛/바디감 별점과 코멘트)을 남기면, MCP 서버를 통해 AI 에이전트가 그 히스토리를 읽고 다음 추출을 다이얼인해주는 개인 프로젝트입니다. "어제보다 신맛을 줄이고 싶다"고 하면 에이전트가 분쇄도를 한 클릭 조이고 물온도를 1도 올린 다음 버전의 레시피를 저장해 줍니다. 데이터라고 해봐야 레시피 수십 건, 피드백 수십 건 — DB 입장에서는 아무것도 아닌 규모입니다. 그런데도 이사를 두 번 했습니다.

## 1막: CouchDB — 로컬에서 시작하다

처음 저장소를 고를 때의 논리는 ADR에 그대로 남아 있습니다(`docs/decisions/0002-couchdb-foundation.md`). 요약하면 이렇습니다.

- 데이터가 자연스럽게 문서 모양입니다. 레시피는 중첩된 파라미터와 스텝 배열을, 피드백은 중첩된 별점 객체를 품고 있어서, 이 규모에서 관계형으로 분해하는 건 "payoff 없는 요식행위"라고 판단했습니다.
- CouchDB는 안정적인 HTTP API를 그대로 노출합니다. 웹 앱, MCP 서버, 에이전트가 전부 같은 프로토콜로 붙을 수 있고 드라이버 브리징이 필요 없습니다.
- `_rev` 기반 낙관적 동시성 제어가 1급 시민입니다. 에이전트의 쓰기와 수동 편집이 충돌할 수 있는 구조에 맞다고 봤습니다.
- PouchDB(브라우저용 CouchDB 호환 스토어)로 나중에 오프라인 동기화로 갈 길이 열려 있었습니다.

SDK도 쓰지 않았습니다. BrewDial이 실제로 쓰는 CouchDB API는 서버 정보 확인, DB 보장, 문서 읽기/쓰기, 목록 조회 정도라서 손으로 짠 `fetch` 클라이언트가 200줄 아래로 끝났고, 의존성이 없으니 감사할 것도 버전 드리프트도 없다는 계산이었습니다. 이 판단은 지금 봐도 맞았다고 생각합니다. 어차피 떠날 DB의 SDK 사용법을 익히는 데 시간을 쓰지 않은 셈이 됐으니까요.

운영 환경은 더 소박했습니다. 집에 있는 맥북에서 CouchDB와 SvelteKit 앱을 launchd로 띄우고, Cloudflare Tunnel로 외부에서 접근할 수 있게 했습니다. 개인 프로젝트 초기 단계로는 충분했고, 실제로 두 달 동안 이 구성으로 매일 아침 레시피를 기록하며 푸어링 타이머니 피드백 폼이니 하는 기능을 붙여 나갔습니다.

이 시절의 데이터는 이렇게 생겼습니다. 타입 필드로 구분되는 문서들이 한 DB에 섞여 살았고, 스키마는 TypeScript 타입 정의가 전부였습니다.

```
recipe:COF-0042      → { type: "recipe", code, method, params: {...}, steps: [...], ... }
feedback:...         → { type: "feedback", recipeCode, ratings: {...}, comment, ... }
preference:global    → { type: "preference", likes: [...], dislikes: [...] }
counter:recipe       → { type: "counter", value: 42 }
```

균열은 두 군데서 왔습니다.

첫 번째는 작은 균열입니다. 위 목록의 마지막 줄, 카운터 문서가 문제였습니다. 레시피에 `COF-0001`, `COF-0002` 같은 순차 코드를 붙이고 싶었는데, CouchDB에는 시퀀스가 없습니다. 그래서 카운터 문서를 읽고 +1 해서 다시 쓰고, 다른 쓰기와 충돌하면 409를 받고 재시도하는 루프를 짰습니다. 나중에 Postgres로 옮기면서 스키마 파일 주석에 이렇게 적었습니다.

```sql
-- Maps the former CouchDB document model to relational tables:
--   recipe:*    -> recipes
--   feedback:*  -> feedback
--   preference:global -> preferences (single row, id='global')
--   counter:recipe (read-modify-write + 409 retry) -> recipe_code_seq (atomic)
```

read-modify-write + 409 재시도가 `create sequence` 한 줄이 되는 순간, 문서 모델을 고른 대가를 처음 체감했습니다.

두 번째는 이사를 강제한 균열입니다. 이 앱을 토스 앱 안의 미니앱(Apps in Toss)으로 올리기로 하면서 전제가 무너졌습니다. 미니앱은 서버 없는 정적 SPA 번들(`.ait`)로 배포됩니다. 즉 브라우저(정확히는 WebView)가 데이터 계층에 직접 붙어야 하는데, 제 CouchDB는 맥북 위에 있었고, ADR에도 적어놨듯 브라우저 → CouchDB 직결은 사용자별 계정을 파거나 공유 자격증명을 클라이언트에 노출하는 것 중 하나를 골라야 했습니다. 게다가 잠들면 서비스도 같이 잠드는 맥북을 불특정 사용자가 여는 앱의 오리진으로 둘 수는 없었습니다.

정리하면 CouchDB가 못나서가 아니라, "내 SvelteKit 서버 뒤의 개인 저장소"라는 전제가 "공개된 정적 앱이 직접 붙는 호스팅 DB"라는 요구로 바뀐 겁니다. 그렇게 첫 이사가 시작됐습니다.

## 2막: Supabase — 문서를 테이블에 눕히기

Supabase를 고른 이유는 명확했습니다. 무료이고, PostgREST 덕에 브라우저에서 anon 키로 바로 붙을 수 있고, RLS(Row Level Security)로 익명 쓰기 규칙을 서버 없이 강제할 수 있고, 밑바닥이 진짜 Postgres라 시퀀스와 CHECK 제약이 공짜로 생깁니다. "서버 없는 정적 SPA + 호스팅 DB" 요구에 이보다 잘 맞는 조합을 찾기 어려웠습니다.

문제는 데이터를 옮기는 쪽이었습니다. 문서 모델 두 달치의 자유는 관계형으로 눕히는 순간 전부 청구서가 되어 돌아왔습니다. 마이그레이션 스크립트(`supabase/migrate-couch-to-supabase.mjs`)의 헤더가 그 청구서 목록입니다.

```javascript
// One-time migration: CouchDB `coffee` -> Supabase (Postgres).
//
// Imports all recipes/feedback/preference and applies the audit
// tagging (duplicates -> superseded, discarded draft -> archived, smoke/QA
// records -> test, intended-variant lineage via parent_code).
//
// Dry-run by default (prints a plan, writes nothing). Pass --yes to write.
```

CouchDB 시절에는 스키마가 없으니 아무 문서나 들어갔습니다. 스모크 테스트하다 만든 레시피, 버리고 다시 만든 초안, 같은 레시피의 중복 저장본이 진짜 데이터와 뒤섞여 있었습니다. 문서 DB는 그걸 문제 삼지 않지만, 관계형으로 옮기면서 "앱에 보여줄 canonical한 활성 집합"을 정의하려니 전수 감사가 필요했습니다. 결과가 스크립트에 하드코딩된 이 지도들입니다.

```javascript
const SUPERSEDED_BY = {
  'COF-0020': 'COF-0021',
  'COF-0031': 'COF-0032',
  'COF-0025': 'COF-0026',
  'COF-0015': 'COF-0040',
  'COF-0008': 'COF-0012',
};
const ARCHIVED = { 'COF-0011': 'COF-0012' }; // discarded draft, replaced by 0012
const TEST = new Set(['COF-0001', 'COF-0006']); // MacBook smoke, QA records
```

레시피 하나하나를 열어보고 "이건 중복이니 superseded, 이건 버린 초안이니 archived, 이건 테스트니 test"를 사람이 판정한 흔적입니다. 스키마가 안 막아준 2개월의 자유를 마이그레이션 날 하루에 몰아서 갚은 셈입니다.

변환 자체도 잔손이 많았습니다. camelCase 문서 필드를 snake_case 컬럼으로 옮기고, 없을 수 있는 필드마다 기본값을 정하고, 부모 레시피가 없는 고아 피드백은 걸러냈습니다.

```javascript
function recipeRow(d) {
  return {
    code: d.code,
    method: d.method,
    version: typeof d.version === 'number' ? d.version : 1,
    params: d.params ?? {},
    steps: d.steps ?? [],
    bean_snapshot: d.beanSnapshot ?? null,
    adjustment_from_previous: d.adjustmentFromPrevious ?? null,
    created_by: d.createdBy === 'agent' ? 'agent' : 'manual',
    status: statusFor(code),
    // ...
  };
}
```

마지막 함정은 시퀀스였습니다. 기존 레시피는 코드를 명시해서 import하므로 시퀀스가 전진하지 않습니다. 그대로 두면 다음 신규 레시피가 `COF-0001`을 다시 뽑으려다 unique 제약에 부딪힙니다. 그래서 import 후 최대 코드 번호로 `setval`을 쏘는 단계가 스크립트 끝에 붙어 있고, RPC 호출이 실패하면 사람이 SQL Editor에서 직접 돌릴 한 줄을 안내하고 끝나도록 했습니다.

```javascript
console.log(`Advancing recipe_code_seq to ${maxCode}...`);
const seq = await supabase.rpc('set_recipe_code_seq', { n: maxCode });
if (seq.error) {
  console.warn('  rpc set_recipe_code_seq failed: ' + seq.error.message);
  console.warn(`  → run once in Supabase SQL editor: select setval('recipe_code_seq', ${maxCode}, true);`);
}
```

문서 DB에서 관계형으로 넘어오는 분이라면 이 세 가지 — 쓰레기 데이터 감사, 필드 이름/기본값 정규화, 시퀀스 재동기화 — 는 거의 반드시 만나게 됩니다.

스크립트는 dry-run을 기본값으로 만들었습니다. 인자 없이 실행하면 무엇을 어떻게 옮길지 계획만 출력하고 아무것도 쓰지 않으며, `--yes`를 붙여야 실제로 씁니다. 그리고 `--yes`는 기존 데이터를 지우고 처음부터 다시 넣는 clean import라 몇 번을 다시 돌려도 결과가 같습니다. 마이그레이션 스크립트를 짤 일이 있다면 이 두 가지(dry-run 기본 + 재실행 가능)는 꼭 넣으시길 권합니다. 실제로 감사 태깅을 다듬느라 여러 번 돌렸는데, 재실행이 무서운 스크립트였다면 그때마다 식은땀을 흘렸을 겁니다.

이사를 마치고 나니 Supabase는 기대만큼 좋았습니다. SQL Editor에서 스키마를 부으면 즉시 REST API가 생기고, RLS로 "익명 사용자는 수동 레시피와 피드백만 쓸 수 있다"는 규칙을 정책으로 박을 수 있었습니다. CouchDB 시절 409 재시도 루프였던 레시피 코드 채번은 Postgres 시퀀스의 default 표현식 한 줄이 됐습니다.

```sql
create sequence if not exists recipe_code_seq start 1;

create table if not exists recipes (
  id   uuid primary key default gen_random_uuid(),
  code text unique not null
         default ('COF-' || lpad(nextval('recipe_code_seq')::text, 4, '0')),
  -- ...
);
```

MCP 서버도 service_role 키로 같은 DB에 붙여서, 에이전트가 만든 레시피가 배포된 미니앱에 즉시 나타나는 구조가 하루 만에 완성됐습니다. 아침에 피드백을 남기면 에이전트가 다음 버전 레시피를 저장하고, 그게 토스 앱 안에서 바로 열립니다.

무료 티어의 제약도 물론 있습니다. 잘 알려진 것으로 일정 기간 요청이 없으면 프로젝트가 일시정지되는 정책(다시 들어가서 깨워야 합니다), DB 용량 제한, 동시 프로젝트 수 제한이 있습니다. 매일 아침 커피를 내리는 앱이라 일시정지에 당한 적은 없었지만, "내가 안 쓰면 잠드는 DB"라는 성질 자체가 에이전트가 아무 때나 붙는 시스템과는 미묘하게 안 맞는 긴장이었습니다.

그런데 정작 두 번째 이사의 방아쇠는 무료 티어가 아니었습니다.

## 3막: OCI PostgreSQL — 백엔드가 없다는 사실을 인정하기

이사 다음 날, 다음 버전(토스 로그인 연동) 설계를 시작하면서 설계 문서 첫 줄에 이렇게 쓰게 됐습니다.

> BrewDial은 현재 **백엔드가 없다.** 브라우저(miniapp/web)는 anon 키, MCP는 service_role 키로 각자 Supabase PostgREST에 직접 붙는다. PostgREST + RLS + 16개 SECURITY DEFINER 함수가 곧 API이자 인증 레이어다.

Supabase가 편했던 이유가 그대로 부채 목록이 된 겁니다. 백엔드를 안 만들어도 되는 게 아니라, 백엔드가 해야 할 일(인증, 권한, 비즈니스 규칙)이 RLS 정책과 SECURITY DEFINER 함수 열여섯 개로 흩어져 DB 안에 살고 있었습니다. 익명 앱일 때는 그걸로 충분했지만, 진짜 사용자 인증을 얹으려니 커스텀 JWT 마스터키 관리, RLS 우회 경로 같은 어려운 문제가 전부 이 구조 위에서 풀어야 하는 문제로 나타났습니다. 지금 Supabase 위에 인증을 쌓으면, 언젠가 자체 백엔드로 갈 때 같은 걸 두 번 만들게 된다는 계산이 섰습니다.

그래서 세 번째 이사는 DB만의 이사가 아니라 아키텍처 이사였습니다. Oracle Cloud의 Always Free Ampere A1 인스턴스(aarch64, 2 vCPU / 10GB RAM — 무료 범위 안에서 이 정도가 나옵니다)에 플레인 PostgreSQL 17과 TypeScript 백엔드(Hono)를 올리고, 신뢰 경계를 "DB 안의 정책"에서 "백엔드 서비스 계층"으로 옮겼습니다. 브라우저가 들고 있던 anon 키와 MCP가 들고 있던 service_role 키는 사라지고, 모든 클라이언트가 백엔드 HTTP API를 경유합니다.

![CouchDB에서 Supabase를 거쳐 OCI PostgreSQL까지의 여정](images/brewdial_db_journey.png)
*세 정거장의 공통점: 이사 당일까지는 다음 이사 계획이 없었다는 것*

자체 운영이라고 해서 거창한 건 없습니다. Docker도 안 씁니다. systemd 유닛 하나가 전부입니다(`deploy/oci/brewdial-api.service`).

```ini
[Unit]
Description=BrewDial API
After=network.target postgresql-17.service
Wants=postgresql-17.service

[Service]
Type=simple
User=opc
WorkingDirectory=/opt/brewdial/apps/api
EnvironmentFile=/etc/brewdial/api.env
ExecStart=/usr/bin/node /opt/brewdial/apps/api/dist/server.js
Restart=on-failure
RestartSec=3
```

시크릿은 리포에 커밋하지 않고 박스의 `/etc/brewdial/api.env`(권한 600)에만 둡니다. 리포에는 값 없이 키 이름만 담은 예시 파일을 커밋해서, 어떤 시크릿이 필요한지는 코드 리뷰로 보이되 값은 절대 남지 않게 했습니다.

```bash
# /etc/brewdial/api.env  (chmod 600, owned by the service user)
DATABASE_URL=postgres://brewdial_app:CHANGE_ME@127.0.0.1:5432/brewdial
PORT=3020
AGENT_TOKEN=CHANGE_ME   # MCP 서버가 쓰는 에이전트 표면 bearer 토큰
NODE_ENV=production
```

DB 롤도 최소 권한으로 갔습니다. 앱이 쓰는 `brewdial_app` 롤은 슈퍼유저가 아니고, 스키마 변경(DDL)은 마이그레이션 도구를 별도로 돌릴 때만 일어납니다. 관리형에서는 콘솔이 대신 그어주던 선을 자체 운영에서는 직접 그어야 합니다.

배포도 거창한 CI 없이 시작했습니다. 로컬 체크아웃에서 rsync로 올리고, 박스에서 빌드하고, systemd 재시작 후 헬스체크를 찌르는 세 줄입니다.

```bash
rsync -az --exclude .git --exclude node_modules ./ opc@<host>:/opt/brewdial/
ssh opc@<host> 'cd /opt/brewdial && pnpm install && \
  pnpm --filter @brewdial/shared --filter @brewdial/db --filter @brewdial/api build && \
  sudo systemctl restart brewdial-api && curl -s localhost:3020/api/health'
```

재미있는 건 네트워크 구성입니다. 이 서버는 <b>인바운드 포트를 하나도 열지 않습니다(SSH 제외).</b> Postgres는 `127.0.0.1`에만 바인딩되고, API 서버도 `127.0.0.1:3020`에만 바인딩됩니다. 외부 서빙은 Cloudflare Tunnel이 맡습니다(`deploy/oci/cloudflared-config.example.yml`).

```yaml
# Hides the OCI origin: cloudflared makes OUTBOUND connections to Cloudflare,
# so NO inbound port (3020) needs opening in firewalld / the OCI security list.
tunnel: <TUNNEL_ID>
credentials-file: /home/opc/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: api.brewdial.<your-domain>
    service: http://127.0.0.1:3020
  - service: http_status:404
```

cloudflared 데몬이 Cloudflare 엣지로 아웃바운드 연결을 먼저 걸어두면, 외부 트래픽은 그 터널을 타고 들어옵니다. 방화벽(firewalld)에는 ssh만 열려 있고 3020도 5432도 없습니다. 서버의 공인 IP를 포트 스캔해도 아무것도 안 보이는데 서비스는 전 세계에서 열립니다. TLS와 DDoS 방어는 Cloudflare 엣지가 처리하니 인증서 갱신 걱정도 없습니다. 사실 이 패턴은 CouchDB 시절 맥북에서 이미 쓰던 것이라, 세 번째 이사에서는 검증된 도구를 그대로 재사용한 셈입니다. 무료로 실배포하는 사이드 프로젝트에서 루프백 바인딩 + Tunnel 조합은 정말 강력하게 추천합니다.

### 두 번째 데이터 이전 — 이번에는 트럭만 갈아탔습니다

두 번째 데이터 이전은 첫 번째보다 훨씬 수월했습니다. Postgres에서 Postgres로 가는 이사라 스키마에서 Supabase 전용 부분(RLS 정책, anon/service_role GRANT, PostgREST가 주입하던 JWT claim 의존)만 걷어내면 테이블·트리거·시퀀스가 그대로 이식됩니다. 전용 로더로 리허설을 먼저 돌려 14개 테이블 전부(레시피 61건, 피드백 15건, 원두 17건 등)의 행 수와 속성 보존을 확인해 두었습니다. 문서 → 관계형이 이사라면, 관계형 → 관계형은 가구 배치 그대로 트럭만 갈아타는 느낌입니다.

컷오버 설계에서 배운 것도 하나 적어둡니다. 원래 계획은 "빅뱅 전환은 불가능하다"는 전제에서 출발했습니다. 웹은 새로 빌드해 배포하면 즉시 전환되지만, 토스 미니앱 번들(`.ait`)은 API 주소가 빌드 타임에 인라인되는 데다 심사와 전파 지연이 있어서 원자적으로 못 바꿉니다. 구버전 앱이 옛 DB에 쓰기를 계속하면 데이터가 두 갈래로 갈라지는(split-brain) 사고가 나므로, 설계에는 "컷오버 순간 Supabase 쪽 쓰기 권한을 서버에서 회수(REVOKE)해 강제 동결하고, 읽기 전용 폴백으로 유지한다"는 단계까지 들어 있었습니다. 실제로는 사용자가 사실상 저 혼자인 앱이라 컷오버 중 쓰기가 없음을 확인하고 동결 단계는 건너뛰었지만, 클라이언트 배포를 내 마음대로 못 하는 채널(앱 심사)이 하나라도 끼어 있으면 DB 이사는 "옛 DB를 언제 지울 수 있는가"의 문제가 된다는 걸 이때 배웠습니다. 전환 자체는 최종 로더 재실행 → 웹 빌드의 API 주소 교체 → MCP 토큰 교체 순서로 하루에 끝났고, 롤백 경로(이전 웹 리비전 재배포 + Supabase 쓰기 권한 복원)도 컷오버 전에 문서로 박아뒀습니다.

### 관리형이 해주던 일이 보이기 시작했다

플레인 Postgres로 오니 그동안 Supabase(정확히는 PostgREST와 클라이언트 SDK)가 조용히 해주던 일들이 하나씩 수면 위로 올라왔습니다. 대표적인 게 jsonb 직렬화입니다. supabase-js로 insert할 때는 JS 배열을 jsonb 컬럼에 그대로 넘겨도 알아서 처리됐는데, 자체 백엔드의 드라이버에서는 객체 배열이 Postgres 배열 리터럴로 해석되면서 `22P02`(invalid input syntax) 에러가 났습니다. 레시피의 `steps` 배열을 넣는 경로에서 터졌고, jsonb 컬럼에는 `JSON.stringify`를 명시적으로 거치도록 고쳐야 했습니다. 헬스체크도 마찬가지입니다. 관리형일 때는 대시보드가 알려주던 "DB 살아있음"을 이제 `GET /api/health`(프로세스 생존)와 `GET /api/db/health`(`select 1` 성공) 두 엔드포인트로 직접 만들어 외부 모니터가 찌르게 해야 했습니다. 하나하나는 사소하지만, 관리형 요금이 공짜가 아니었다는 걸 몸으로 확인하는 과정이었습니다.

솔직하게 적어야 할 리스크도 있습니다. <b>백업이 아직 없습니다.</b> 런북에는 `pg_dump`를 크론으로 돌려 오브젝트 스토리지에 올리고 복원 테스트까지 통과해야 Supabase를 폐기할 수 있다고 게이트를 박아뒀지만, 이 글을 쓰는 시점에는 그 단계 전입니다. 그래서 구 Supabase 프로젝트를 read-only 폴백 겸 사실상의 백업으로 아직 살려두고 있습니다. 관리형 DB에서 자체 운영으로 넘어오는 순간 백업은 "설정 체크박스"에서 "내가 안 하면 없는 것"으로 바뀝니다. 이게 자체 운영의 진짜 비용입니다.

## 세 정거장 비교

세 구성 모두 금전 비용은 0원이었습니다. 차이는 돈이 아니라 다른 축에서 났습니다.

| | CouchDB (맥북 로컬) | Supabase 무료 티어 | OCI PostgreSQL (자체 운영) |
|---|---|---|---|
| 금전 비용 | 0원 | 0원 | 0원 (Always Free) |
| 셋업 난이도 | 낮음 (brew install 수준) | 매우 낮음 (콘솔 클릭) | 높음 (인스턴스+PG+systemd+Tunnel) |
| 상시 가용성 | 없음 (맥북이 자면 죽음) | 높음 (단, 미사용 시 일시정지) | 높음 (인스턴스 회수 정책은 유의) |
| 브라우저 직접 접근 | 사실상 불가 (자격증명 노출) | 가능 (anon 키 + RLS) | 불가 — 백엔드 경유가 설계 |
| 인증/권한의 위치 | 없음 (개인용 전제) | DB 안 (RLS + DEFINER 함수) | 백엔드 서비스 계층 |
| 운영 부담 | 거의 없음 | 거의 없음 | 패치·백업·모니터링 전부 내 몫 |
| 데이터 이전 탈출 비용 | 높음 (문서→관계형 전수 감사) | 낮음 (pg_dump로 그대로) | — (다음 이사도 Postgres끼리) |

표에서 하나만 꼽으라면 마지막 줄입니다. Supabase의 밑이 진짜 Postgres라는 사실은 쓸 때는 별 감흥이 없다가 떠날 때 빛났습니다. 반대로 CouchDB는 떠나는 날 가장 비쌌습니다. <b>DB를 고를 때 "들어가기 쉬운가"만큼 "나올 때 얼마인가"를 봐야 한다는 것</b>을 이 프로젝트에서 배웠습니다.

## 마치며: 관리형이냐 자체 운영이냐는 정답이 아니라 단계의 문제

세 번의 이사를 겪고 나서 내린 결론은, 세 선택 모두 그 시점에는 옳았다는 겁니다.

- 혼자 쓰는 프로토타입 단계에서 로컬 CouchDB는 옳았습니다. 클라우드 계정도 스키마 설계도 없이 그날 저녁부터 레시피를 기록할 수 있었습니다.
- 남에게 보여줘야 하는 단계에서 Supabase는 옳았습니다. 서버 한 줄 없이 정적 앱을 실사용자에게 배포할 수 있는 가장 빠른 길이었습니다.
- 인증과 비즈니스 규칙이 생기는 단계에서 자체 백엔드 + 플레인 PostgreSQL은 옳았습니다. 신뢰 경계를 코드로 소유하게 됐고, 다음 기능(로그인)을 한 번만 만들면 됩니다.

틀린 건 선택이 아니라 "이 선택이 끝까지 갈 것"이라는 가정이었습니다. 그래서 사이드 프로젝트의 DB 결정에서 정말 중요한 건 완벽한 첫 선택이 아니라, 다음 이사를 싸게 만드는 습관이라고 생각합니다. 저에게는 그게 세 가지였습니다. 왜 이걸 골랐는지 ADR로 남기기(덕분에 "그때는 맞았다"를 확인하며 미련 없이 떠날 수 있었습니다), 마이그레이션 스크립트를 dry-run 기본으로 만들기, 그리고 되도록 표준 SQL 위에 서 있기.

규모별로 추천을 정리하면 이렇습니다. 나 혼자 쓰고 노트북이 꺼져도 되는 단계면 로컬 DB(SQLite든 CouchDB든)로 충분합니다. 남이 쓰기 시작하면 관리형 무료 티어(Supabase류)로 가되, 인증과 권한 로직을 그 플랫폼 전용 기능에 깊게 묻지 않는 걸 권합니다. 그리고 백엔드를 어차피 만들게 되는 시점 — 보통 "진짜 로그인"이 필요해지는 시점 — 이 오면, Oracle Always Free 같은 상시 무료 컴퓨트에 플레인 PostgreSQL + Cloudflare Tunnel 조합이 0원으로 갈 수 있는 가장 어른스러운 구성이었습니다. 단, 그날부터 백업은 당신 몫입니다. 저처럼 "백업은 다음 주에"라고 미루고 있다면, 최소한 이전 DB를 지우지는 마세요. 그게 지금 제 백업입니다.

---

**참고 자료:**
- [Oracle Cloud Always Free 리소스](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Supabase 공식 문서](https://supabase.com/docs)
- [Cloudflare Tunnel 공식 문서](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
- [Apache CouchDB 공식 문서](https://docs.couchdb.org/)
- BrewDial 저장소 — 공개 준비 중입니다. 공개되면 링크를 추가하겠습니다.

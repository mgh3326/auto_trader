import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import {
  declareExternalCash,
  ExternalCashDeclareConflict,
  fetchExternalCashCurrent,
  fetchExternalCashForm,
  fetchExternalCashHistory,
  fetchFundingAdvisories,
  fetchFundingAdvisory,
  fetchFundingAllocation,
} from "../api/fundingAdvisory";
import { PageSafetyNote } from "../components/PageSafetyNote";
import { DesktopShell } from "../desktop/DesktopShell";
import { formatRelativeTime } from "../format/relativeTime";
import { Button, Card, Pill } from "../ds";
import { useViewport } from "../hooks/useViewport";
import { MobileShell } from "../mobile/MobileShell";
import type {
  ExternalCashCurrentView,
  ExternalCashDeclaration,
  ExternalCashForm,
  ExternalCashHeadsView,
  ExternalCashHistoryView,
  FundingAdvisoryView,
  FundingAllocationView,
  FundingRouteView,
} from "../types/fundingAdvisory";
import { EXTERNAL_CASH_NO_AUTO_ADD_NOTICE } from "../types/fundingAdvisory";
import "../styles/funding.css";

const NON_CTA_LABEL = "경로 설명 · 이 화면에서 주문 안 만듦";
const SELL_ROUTE_IDS = new Set(["PROFITABLE_TRIM", "LOSS_CUT_ROTATION"]);

interface FundingPageData {
  advisories: FundingAdvisoryView[];
  detail: FundingAdvisoryView | null;
  allocation: FundingAllocationView;
  external: ExternalCashHeadsView;
  history: ExternalCashHistoryView;
  declarationForm: ExternalCashForm | null;
}

function formatAmount(value: string | null, currency: string): string {
  if (value === null) return "금액 미상";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return `${value} ${currency}`;
  return `${new Intl.NumberFormat(currency === "KRW" ? "ko-KR" : "en-US", {
    maximumFractionDigits: currency === "KRW" ? 0 : 2,
  }).format(parsed)} ${currency}`;
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("ko-KR");
}

function SafetyBoundary({ position }: { position: "top" | "bottom" }) {
  return (
    <PageSafetyNote
      routeId={`funding-${position}`}
      heading="조회·검토 전용 자금 조달 권고"
      tag="자동 실행 없음"
      dismissible={false}
      items={[
        "이 화면은 입금·환전·차입·매도·주문을 실행하거나 proposal을 만들지 않습니다.",
        "외부 현금 선언은 broker 잔고 증거가 아니며 주문가능액·사이징·cap·승인 판단에 쓰이지 않습니다.",
        "페이지 조회는 revision을 재계산할 수 있지만 Telegram 발송 조건이 아닙니다.",
      ]}
    />
  );
}

function Metric({ label, value, emphasis = false }: { label: string; value: ReactNode; emphasis?: boolean }) {
  return (
    <div className={`funding-metric${emphasis ? " funding-metric--emphasis" : ""}`}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function RouteCard({ route, currency }: { route: FundingRouteView; currency: string }) {
  const isSellPath = SELL_ROUTE_IDS.has(route.route_id);
  return (
    <article className="funding-route" data-testid={`funding-route-${route.route_id}`}>
      <div className="funding-route__head">
        <div>
          <h3>{route.label}</h3>
          <p>{formatAmount(route.route_fundable_amount, currency)}</p>
        </div>
        <Pill tone={route.comparison === "dominated" ? "warn" : "paper"} size="sm">
          {route.comparison}
        </Pill>
      </div>
      <dl className="funding-route__axes">
        <Metric label="금액 상태" value={route.amount_status} />
        <Metric label="근거 신뢰" value={route.confidence} />
        <Metric label="기한" value={route.deadline_status} />
        <Metric label="예상 시간" value={route.eta_minutes === null ? "미상" : `${route.eta_minutes}분`} />
        <Metric label="가역성" value={route.reversibility} />
        <Metric label="적격성" value={route.eligibility} />
      </dl>
      {isSellPath ? (
        <div className="funding-non-cta" data-testid="funding-non-cta">
          <strong>{NON_CTA_LABEL}</strong>
          <span>별도의 create 확인 뒤 기존 승인·veto 경로로만 진입합니다.</span>
        </div>
      ) : null}
    </article>
  );
}

export function FundingAdvisoryDetail({ advisory }: { advisory: FundingAdvisoryView }) {
  const { target, need, trigger } = advisory;
  return (
    <div className="funding-detail" data-testid="funding-advisory-detail">
      <Card>
        <div className="funding-card-head">
          <div>
            <div className="funding-eyebrow">후보 1건의 broker-authoritative 관측</div>
            <h2>{target.symbol}</h2>
            <p>{target.market} · {target.account_mode} · {target.currency}</p>
          </div>
          <Pill tone="warn">shortfall</Pill>
        </div>
        <dl className="funding-metrics">
          <Metric label="필요 현금" value={formatAmount(need.required_cash, target.currency)} />
          <Metric label="target broker 주문가능액" value={formatAmount(need.target_buying_power, target.currency)} />
          <Metric label="이 후보 shortfall" value={formatAmount(need.shortfall, target.currency)} emphasis />
          <Metric label="다른 pending limit buy" value={formatAmount(need.other_pending_required, target.currency)} />
          <Metric label="별도 reserved" value={formatAmount(need.reserved_cash, target.currency)} />
          <Metric
            label="pending/reserved 포함 운영상 gap"
            value={formatAmount(need.operational_gap_including_other_pending, target.currency)}
            emphasis
          />
        </dl>
        <p className="funding-disclosure">
          shortfall은 이 후보 1건의 required cash − target buying power입니다. 다른 대기 매수와 reserved는
          숨기지 않고 별도 행 및 운영상 gap에 표시합니다.
        </p>
      </Card>

      <Card>
        <div className="funding-section-head">
          <div>
            <h2>조달 경로 비교</h2>
            <p>단일 점수가 아니라 금액·비용·시간·실현 영향·가역성 축으로 비교합니다.</p>
          </div>
        </div>
        <div className="funding-route-grid">
          {advisory.routes.map((route) => <RouteCard key={route.route_id} route={route} currency={target.currency} />)}
        </div>
      </Card>

      <Card soft>
        <div className="funding-section-head">
          <div>
            <h2>부분 조달 참고 시나리오</h2>
            <p>같은 다축 비교 결과를 공개하는 참고안이며 selected가 아닙니다.</p>
          </div>
          <Pill tone="paper">selected: false</Pill>
        </div>
        {advisory.combination.legs.length ? (
          <ol className="funding-combination">
            {advisory.combination.legs.map((leg) => (
              <li key={`${leg.route_id}:${leg.cumulative_planned_amount}`}>
                <span>{leg.route_id}</span>
                <strong>{formatAmount(leg.planned_amount, target.currency)}</strong>
                <small>남은 gap {formatAmount(leg.remaining_gap, target.currency)}</small>
              </li>
            ))}
          </ol>
        ) : <p className="funding-muted">비교 가능한 조합이 없습니다.</p>}
      </Card>

      <Card>
        <div className="funding-section-head">
          <div>
            <h2>상류 gate와 기존 승인 경로</h2>
            <p>gate version은 평가별 해시가 아닌 계약·스키마 버전입니다.</p>
          </div>
        </div>
        <dl className="funding-metrics funding-metrics--compact">
          <Metric label="gate" value={`${trigger.gate_name} · ${trigger.gate_version}`} />
          <Metric label="gate 통과 시각" value={formatTime(trigger.gate_evaluated_at)} />
          <Metric label="유효 종료" value={formatTime(trigger.valid_until)} />
          <Metric label="provenance" value="분류·사이징·eligibility 입력 아님" />
        </dl>
        <div className="funding-handoff">
          <strong>{advisory.proposal_handoff.action_label}</strong>
          <p>{advisory.proposal_handoff.ordinary_trim}</p>
          <p>{advisory.proposal_handoff.loss_cut}</p>
          <p>create 확인은 곧 기존 승인·veto 경로 진입입니다. funding 예외 태그는 없습니다.</p>
        </div>
      </Card>
    </div>
  );
}

function AdvisoryList({ advisories }: { advisories: FundingAdvisoryView[] }) {
  return (
    <section className="funding-section">
      <div className="funding-section-head">
        <div><h2>활성 shortfall 권고</h2><p>상류 비-자금 gate를 모두 통과한 후보만 표시합니다.</p></div>
        <Pill tone="paper">{advisories.length}건</Pill>
      </div>
      {advisories.length === 0 ? <Card soft><p className="funding-muted">현재 활성 권고가 없습니다.</p></Card> : (
        <div className="funding-advisory-list">
          {advisories.map((advisory) => (
            <Card key={advisory.advisory_id}>
              <div className="funding-card-head">
                <div>
                  <div className="funding-eyebrow">{advisory.target.market} · {advisory.target.account_mode}</div>
                  <h2>{advisory.target.symbol}</h2>
                  <p>이 후보 shortfall {formatAmount(advisory.need.shortfall, advisory.target.currency)}</p>
                </div>
                <Pill tone="warn">active</Pill>
              </div>
              <dl className="funding-metrics funding-metrics--compact">
                <Metric label="다른 pending" value={formatAmount(advisory.need.other_pending_required, advisory.target.currency)} />
                <Metric label="reserved" value={formatAmount(advisory.need.reserved_cash, advisory.target.currency)} />
                <Metric label="운영상 gap" value={formatAmount(advisory.need.operational_gap_including_other_pending, advisory.target.currency)} />
                <Metric label="평가 시각" value={formatTime(advisory.evaluated_at)} />
              </dl>
              <Link className="funding-read-link" to={`/funding/${advisory.advisory_id}`}>권고 상세 열기 · 읽기 전용</Link>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

function AllocationView({ allocation }: { allocation: FundingAllocationView }) {
  return (
    <section className="funding-section">
      <div className="funding-section-head">
        <div><h2>시장 간 배분 뷰</h2><p>통화별 원 단위를 유지하며 실행 가능한 FX 없이 KRW+USD를 합산하지 않습니다.</p></div>
      </div>
      <div className="funding-buckets">
        {allocation.buckets.map((bucket) => (
          <Card key={bucket.currency} soft>
            <div className="funding-card-head">
              <h3>{bucket.currency} native bucket</h3>
              {bucket.contention ? <Pill tone="warn">중복 수요 주의</Pill> : null}
            </div>
            <dl className="funding-metrics funding-metrics--compact">
              <Metric label="broker 확인액" value={formatAmount(bucket.broker_confirmed_total_native, bucket.currency)} />
              <Metric label="운영자 선언액" value={formatAmount(bucket.declared_total_native, bucket.currency)} />
              <Metric label="조건부 금액" value={formatAmount(bucket.conditional_total_native, bucket.currency)} />
              <Metric label="표시 합계" value={formatAmount(bucket.display_total_native_including_declared, bucket.currency)} />
            </dl>
            <p className="funding-disclosure">선언액은 표시용이며 자동판단 입력이 아닙니다.</p>
          </Card>
        ))}
        {allocation.buckets.length === 0 ? <Card soft><p className="funding-muted">표시할 통화 bucket이 없습니다.</p></Card> : null}
      </div>
    </section>
  );
}

const NEW_LOCATION = "__new__";

function locationCards(heads: ExternalCashCurrentView[]): ExternalCashCurrentView[] {
  const hasParking = heads.some((view) => view.current?.location_key === "parking_primary");
  if (hasParking) return heads;
  return [
    {
      status: "missing",
      amount_status: "unknown",
      current: null,
      route_fundable_amount: null,
      verification_badge: "운영자 선언 · 시스템 검증 불가",
      warning_code: "external_cash_missing",
    },
    ...heads,
  ];
}

function locationKeyFromLabel(label: string): string {
  const ascii = label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return ascii || "parking_primary";
}

export function ExternalCashPanel({
  current,
  history,
  form,
  onSaved,
}: {
  current: ExternalCashHeadsView;
  history: ExternalCashHistoryView;
  form: ExternalCashForm | null;
  onSaved: () => Promise<void>;
}) {
  const cards = locationCards(current.heads);
  const [locationChoice, setLocationChoice] = useState(form?.default_location_key ?? "parking_primary");
  const [newLocationKey, setNewLocationKey] = useState("");
  const [displayLabel, setDisplayLabel] = useState(form?.default_display_label ?? "파킹통장");
  const [currency, setCurrency] = useState<"KRW" | "USD">(form?.default_currency ?? "KRW");
  const [amount, setAmount] = useState(form?.default_amount ?? "0");
  const [sourceNote, setSourceNote] = useState(form?.default_source_note ?? "운영자 선언");
  const [submitState, setSubmitState] = useState<"idle" | "saving" | "saved" | "error" | "conflict">("idle");
  const [conflictHead, setConflictHead] = useState<ExternalCashDeclaration | null>(null);

  useEffect(() => {
    if (!form) return;
    setLocationChoice(form.default_location_key);
    setDisplayLabel(form.default_display_label);
    setCurrency(form.default_currency);
    setAmount(form.default_amount);
    setSourceNote(form.default_source_note);
    setNewLocationKey("");
    setSubmitState("idle");
    setConflictHead(null);
  }, [form]);

  const selectedHead = form?.heads.find(
    (head) =>
      head.location_key === locationChoice
      && head.currency === currency,
  );
  const isNewLocation = locationChoice === NEW_LOCATION;
  const locationKey = isNewLocation
    ? (newLocationKey || locationKeyFromLabel(displayLabel))
    : locationChoice;
  const expectedHead = isNewLocation ? null : (selectedHead?.expected_head_declaration_id ?? null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form) return;
    setSubmitState("saving");
    setConflictHead(null);
    try {
      await declareExternalCash({
        owner_user_id: form.owner_user_id,
        location_key: locationKey,
        display_label: displayLabel,
        currency,
        amount,
        as_of: form.as_of,
        source_note: sourceNote,
        expected_head_declaration_id: expectedHead,
        idempotency_key: form.idempotency_key,
      });
      setSubmitState("saved");
      await onSaved();
    } catch (caught) {
      if (caught instanceof ExternalCashDeclareConflict) {
        setConflictHead(caught.currentHead);
        setSubmitState("conflict");
        return;
      }
      setSubmitState("error");
    }
  }

  return (
    <section className="funding-section" id="external-cash-declaration">
      <div className="funding-section-head">
        <div>
          <h2>외부 현금 선언</h2>
          <p>append-only 운영자 snapshot이며 실제 이체 또는 broker 잔고 확인이 아닙니다. UI의 수정은 새 선언입니다.</p>
        </div>
      </div>
      <p className="funding-disclosure" data-testid="external-cash-notice">
        {EXTERNAL_CASH_NO_AUTO_ADD_NOTICE}
      </p>
      <div className="funding-external-grid">
        {cards.map((view) => {
          const record = view.current;
          const key = record ? `${record.location_key}:${record.currency}` : "parking_primary:KRW";
          return (
            <Card key={key} data-testid={`external-cash-card-${record?.location_key ?? "parking_primary"}`}>
              <dl className="funding-metrics funding-metrics--compact">
                <Metric label="위치" value={record?.display_label ?? "파킹통장"} />
                <Metric label="선언액" value={record ? formatAmount(record.amount, record.currency) : "선언 없음"} />
                <Metric label="통화" value={record?.currency ?? "KRW"} />
                <Metric label="as-of" value={record ? formatTime(record.as_of) : "—"} />
                <Metric label="경과시간" value={record ? (formatRelativeTime(record.as_of) ?? "—") : "—"} />
                <Metric label="상태" value={view.status} />
              </dl>
              <p className="funding-verification">{view.verification_badge}</p>
            </Card>
          );
        })}
      </div>

      <Card>
        <div className="funding-section-head">
          <div>
            <h2>선언 이력</h2>
            <p>원장은 append-only입니다. 수정처럼 보여도 새 행이 추가됩니다.</p>
          </div>
          <Pill tone="paper">{history.count}건</Pill>
        </div>
        {history.declarations.length ? (
          <div className="funding-history-wrap">
            <table className="funding-history-table" data-testid="external-cash-history-table">
              <thead>
                <tr>
                  <th>as-of</th>
                  <th>위치</th>
                  <th>통화</th>
                  <th>금액</th>
                  <th>메모</th>
                </tr>
              </thead>
              <tbody>
                {history.declarations.map((row) => (
                  <tr key={row.declaration_id}>
                    <td>{formatTime(row.as_of)}</td>
                    <td>{row.display_label}</td>
                    <td>{row.currency}</td>
                    <td>{formatAmount(row.amount, row.currency)}</td>
                    <td>{row.source_note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="funding-muted">이력이 없습니다.</p>}
      </Card>

      {form ? (
        <Card>
          <form className="funding-form" onSubmit={submit} data-testid="external-cash-form">
            <div className="funding-form__notice">{EXTERNAL_CASH_NO_AUTO_ADD_NOTICE}</div>
            <label>
              위치
              <select
                value={locationChoice}
                onChange={(event) => {
                  const next = event.target.value;
                  setLocationChoice(next);
                  if (next === NEW_LOCATION) {
                    setDisplayLabel("");
                    setNewLocationKey("");
                    return;
                  }
                  const match = form.heads.find((head) => head.location_key === next && head.currency === currency)
                    ?? form.heads.find((head) => head.location_key === next);
                  if (match) {
                    setDisplayLabel(match.display_label);
                    setAmount(match.amount);
                    setCurrency(match.currency === "USD" ? "USD" : "KRW");
                  }
                }}
              >
                <option value="parking_primary">파킹통장</option>
                {form.heads
                  .filter((head) => head.location_key !== "parking_primary")
                  .map((head) => (
                    <option key={`${head.location_key}:${head.currency}`} value={head.location_key}>
                      {head.display_label}
                    </option>
                  ))}
                <option value={NEW_LOCATION}>신규 위치</option>
              </select>
            </label>
            {isNewLocation ? (
              <label>
                위치 키
                <input
                  value={newLocationKey}
                  onChange={(event) => setNewLocationKey(event.target.value)}
                  placeholder="parking_secondary"
                  required
                />
              </label>
            ) : null}
            <label>
              표시 이름
              <input value={displayLabel} onChange={(event) => setDisplayLabel(event.target.value)} required />
            </label>
            <label>
              통화
              <select
                value={currency}
                onChange={(event) => setCurrency(event.target.value === "USD" ? "USD" : "KRW")}
              >
                <option value="KRW">KRW</option>
                <option value="USD">USD</option>
              </select>
            </label>
            <label>
              금액 ({currency})
              <input value={amount} inputMode="decimal" onChange={(event) => setAmount(event.target.value)} required />
            </label>
            <label>
              as-of (현재시각 고정)
              <time data-testid="external-cash-as-of" dateTime={form.as_of}>{formatTime(form.as_of)}</time>
            </label>
            <label>
              메모
              <input value={sourceNote} onChange={(event) => setSourceNote(event.target.value)} required />
            </label>
            <p className="funding-disclosure">as-of는 서버 현재시각이며 미래 값을 넣을 수 없습니다. 저장은 새 선언 append입니다.</p>
            <Button type="submit" disabled={submitState === "saving"}>
              {submitState === "saving" ? "저장 중…" : "선언 저장 · 돈 이동 아님"}
            </Button>
            {submitState === "saved" ? <p role="status">새 선언을 append했습니다. 실제 이체는 발생하지 않았습니다.</p> : null}
            {submitState === "error" ? <p role="alert">저장하지 못했습니다. head·시각·권한을 다시 확인하세요.</p> : null}
            {submitState === "conflict" ? (
              <div className="funding-conflict" role="alert" data-testid="external-cash-conflict">
                <strong>다른 곳에서 선언이 갱신되었습니다. 새 head를 확인하세요.</strong>
                {conflictHead ? (
                  <p data-testid="external-cash-conflict-head">
                    {conflictHead.display_label} · {formatAmount(conflictHead.amount, conflictHead.currency)} · {formatTime(conflictHead.as_of)}
                  </p>
                ) : <p>현재 head를 다시 불러오세요.</p>}
              </div>
            ) : null}
          </form>
        </Card>
      ) : <Card soft><p className="funding-muted">관리자만 새 선언을 append할 수 있습니다.</p></Card>}
    </section>
  );
}

export function FundingPageContent({ data, onReload }: { data: FundingPageData; onReload: () => Promise<void> }) {
  return (
    <div className="funding-page">
      <SafetyBoundary position="top" />
      <header className="funding-page__header">
        <div>
          <div className="funding-eyebrow">FUNDING ADVISORY</div>
          <h1>자금 조달 권고</h1>
          <p>shortfall의 숫자 근거와 조달 경로를 검토합니다. 실행은 이 화면 밖의 기존 승인 경로에서만 가능합니다.</p>
        </div>
        {data.detail ? <Link className="funding-read-link" to="/funding">전체 권고로 돌아가기</Link> : null}
      </header>
      {data.detail ? <FundingAdvisoryDetail advisory={data.detail} /> : <AdvisoryList advisories={data.advisories} />}
      <AllocationView allocation={data.allocation} />
      <ExternalCashPanel current={data.external} history={data.history} form={data.declarationForm} onSaved={onReload} />
      <SafetyBoundary position="bottom" />
    </div>
  );
}

function FundingPageLoader({ advisoryId }: { advisoryId?: string }) {
  const [data, setData] = useState<FundingPageData | null>(null);
  const [error, setError] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setError(false);
    try {
      const [list, detail, allocation, external, history, declarationForm] = await Promise.all([
        fetchFundingAdvisories(signal),
        advisoryId ? fetchFundingAdvisory(advisoryId, signal) : Promise.resolve(null),
        fetchFundingAllocation(signal),
        fetchExternalCashCurrent(signal),
        fetchExternalCashHistory(signal),
        fetchExternalCashForm(signal),
      ]);
      setData({ advisories: list.advisories, detail, allocation, external, history, declarationForm });
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(true);
    }
  }, [advisoryId]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (error) return <div className="funding-load-state" role="alert">권고 데이터를 불러오지 못했습니다.</div>;
  if (!data) return <div className="funding-load-state" role="status">권고 데이터를 불러오는 중…</div>;
  return <FundingPageContent data={data} onReload={() => load()} />;
}

export function FundingRoute() {
  const { advisoryId } = useParams();
  const viewport = useViewport();
  const content = <FundingPageLoader advisoryId={advisoryId} />;
  return viewport === "mobile" ? (
    <MobileShell title="자금 조달 권고"><div className="funding-mobile-wrap">{content}</div></MobileShell>
  ) : <DesktopShell center={content} />;
}

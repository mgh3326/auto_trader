// 트리거 행 블록 — §144차 요구사항 ①③④.
//
// One card per trigger. Cards rather than a wide table because the operator
// reads this on a phone as often as on a desktop, and each row carries a small
// cluster of related numbers (trigger price, distance, two cash samples).
import { Link } from "react-router-dom";
import { Card, Pill } from "../../ds";
import type {
  ActiveBuyWatchRow,
  ApprovalContext,
  AveragingSampleRow,
  AveragingTriggerRow,
  DiscoveryGateRow,
  SupportNetPlacement,
  SupportNetRow,
  SupportNetTier,
} from "../../types/buyPlan";
import { dateTime, exact, money, pct, price, quantity } from "./format";
import {
  APPROVAL_LANE_LABEL,
  APPROVAL_LANE_LABEL_GATE_OFF,
  APPROVAL_LANE_REASON_LABEL,
  COMPARISON_LABEL,
  FUNDING_BROKER_LABEL,
  GATE_CONDITION_STATE_LABEL,
  GATE_STATE_LABEL,
  PLACEMENT_FORM_LABEL,
  SOURCE_STATE_LABEL,
} from "./labels";

function SectionHeading({
  title,
  subtitle,
}: Readonly<{ title: string; subtitle?: string }>) {
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <h2 style={{ margin: 0, fontSize: 16 }}>{title}</h2>
      {subtitle && (
        <p style={{ margin: 0, fontSize: 12, color: "var(--fg-2)" }}>{subtitle}</p>
      )}
    </div>
  );
}

function EmptyNote({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <Card soft style={{ padding: 14, fontSize: 12, color: "var(--fg-2)" }}>
      {children}
    </Card>
  );
}

function SymbolLink({
  market,
  symbol,
  name,
}: Readonly<{ market: string; symbol: string; name: string | null }>) {
  return (
    <Link
      to={`/stocks/${market}/${encodeURIComponent(symbol)}`}
      style={{ color: "inherit", textDecoration: "none", fontWeight: 700 }}
    >
      {name || symbol}
      <span style={{ color: "var(--fg-2)", fontWeight: 400, marginLeft: 6 }}>
        {symbol}
      </span>
    </Link>
  );
}

// verify-r1 B6: with the master gate off, a cap-passing notional still ends
// up as a manual card, so the badge must not promise an automatic submission
// the operator would sit waiting for.
function LaneBadge({
  lane,
  reason,
  approval,
}: Readonly<{
  lane: AveragingSampleRow["approval_lane"];
  reason: AveragingSampleRow["approval_lane_reason"];
  approval: ApprovalContext;
}>) {
  const gateOff = approval.master_gate_enabled === false;
  const label = gateOff ? APPROVAL_LANE_LABEL_GATE_OFF[lane] : APPROVAL_LANE_LABEL[lane];
  const tone = !gateOff && lane === "auto_submit" ? "accent" : "warn";
  return (
    <span title={`${APPROVAL_LANE_REASON_LABEL[reason]} · ${approval.notice}`}>
      <Pill tone={tone} size="sm">
        {label}
      </Pill>
    </span>
  );
}

export function ApprovalNotice({
  approval,
}: Readonly<{ approval: ApprovalContext }>) {
  return (
    <Card soft style={{ padding: 12, display: "grid", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <Pill
          tone={approval.master_gate_enabled === true ? "accent" : "warn"}
          size="sm"
        >
          {approval.master_gate_enabled === true
            ? "자동승인 마스터 게이트 ON"
            : approval.master_gate_enabled === false
              ? "자동승인 마스터 게이트 OFF"
              : "자동승인 게이트 상태 불명"}
        </Pill>
        <code style={{ fontSize: 11, color: "var(--fg-2)" }}>
          {approval.master_gate_source}
        </code>
      </div>
      <p style={{ margin: 0, fontSize: 12 }}>{approval.notice}</p>
      {approval.unevaluated_conditions.length > 0 && (
        <details>
          <summary style={{ cursor: "pointer", fontSize: 11, color: "var(--fg-2)" }}>
            이 보드가 확인하지 않은 자동제출 조건 (
            {approval.unevaluated_conditions.length})
          </summary>
          <ul
            style={{
              margin: "6px 0 0",
              paddingLeft: 16,
              fontSize: 11,
              color: "var(--fg-2)",
            }}
          >
            {approval.unevaluated_conditions.map((condition) => (
              <li key={condition}>{condition}</li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}

function Field({
  label,
  value,
  title,
}: Readonly<{ label: string; value: string; title?: string }>) {
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <span style={{ fontSize: 11, color: "var(--fg-2)" }}>{label}</span>
      <span
        style={{ fontSize: 13, fontVariantNumeric: "tabular-nums" }}
        title={title}
      >
        {value}
      </span>
    </div>
  );
}

function AveragingCard({
  row,
  approval,
}: Readonly<{ row: AveragingTriggerRow; approval: ApprovalContext }>) {
  return (
    <Card style={{ padding: 16, display: "grid", gap: 12 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <SymbolLink market={row.market} symbol={row.symbol} name={row.symbol_name} />
        <Pill tone={row.turn_point_reached ? "accent" : "paper"} size="sm">
          {row.turn_point_reached ? "전환점 도달" : "전환점 대기"}
        </Pill>
        {!row.within_policy_add_cap && (
          <span title="정책의 시장별 add 상한 밖 — 자금 합계에 포함하지 않습니다.">
            <Pill tone="paper" size="sm">add 상한 밖 · #{row.market_rank}</Pill>
          </span>
        )}
        <Pill tone={row.funding_broker === "unattributed" ? "warn" : "paper"} size="sm">
          {FUNDING_BROKER_LABEL[row.funding_broker]}
        </Pill>
      </div>

      <div
        style={{
          display: "grid",
          gap: 10,
          gridTemplateColumns: "repeat(auto-fit, minmax(96px, 1fr))",
        }}
      >
        <Field
          label={`전환점 P* (k=${row.k})`}
          value={price(row.turn_point_price, row.currency)}
          title={row.turn_point_price}
        />
        <Field label="현재가" value={price(row.current_price, row.currency)} />
        <Field label="전환점까지" value={pct(row.distance_to_turn_point_pct)} />
        <Field label="평단" value={price(row.average_price, row.currency)} />
        <Field label="수량" value={quantity(row.quantity)} />
        <Field label="평가손익" value={pct(row.unrealized_pnl_pct)} />
      </div>

      <div style={{ display: "grid", gap: 6 }}>
        <span style={{ fontSize: 11, color: "var(--fg-2)" }}>
          트리거 도달 시 예상 소요액
        </span>
        {row.samples.map((sample) => (
          <div
            key={sample.offset_from_turn_point_pct}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 10,
              flexWrap: "wrap",
              fontSize: 13,
            }}
          >
            <span style={{ color: "var(--fg-2)" }}>
              전환점 {pct(sample.offset_from_turn_point_pct, 0)} (
              {price(sample.price, row.currency)})
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <strong
                style={{ fontVariantNumeric: "tabular-nums" }}
                title={exact(sample.additional_notional)}
              >
                {money(sample.additional_notional, row.currency)}
              </strong>
              <LaneBadge
                lane={sample.approval_lane}
                reason={sample.approval_lane_reason}
                approval={approval}
              />
            </span>
          </div>
        ))}
      </div>

      {row.notes.length > 0 && (
        <ul
          style={{
            margin: 0,
            paddingLeft: 16,
            fontSize: 11,
            color: "var(--fg-2)",
            display: "grid",
            gap: 2,
          }}
        >
          {row.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function AveragingBlock({
  rows,
  approval,
}: Readonly<{ rows: AveragingTriggerRow[]; approval: ApprovalContext }>) {
  return (
    <section style={{ display: "grid", gap: 12 }}>
      <SectionHeading
        title="① 물타기 A(k) 전환점"
        subtitle="평단이 제안가의 k 이내로 내려오는 데 필요한 금액. 전환점 위에서는 정책상 NO_ORDER."
      />
      {rows.length === 0 ? (
        <EmptyNote>
          물타기 전환점을 계산할 언더워터 보유가 없습니다 (또는 원가·현재가를 확인하지
          못했습니다).
        </EmptyNote>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {rows.map((row) => (
            <AveragingCard
              key={`${row.market}:${row.symbol}`}
              row={row}
              approval={approval}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function PlacementLine({
  placement,
  currency,
}: Readonly<{
  placement: SupportNetPlacement;
  currency: SupportNetRow["currency"];
}>) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        gap: 10,
        flexWrap: "wrap",
        fontSize: 12,
      }}
    >
      <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Pill tone={placement.form === "resting_order" ? "accent" : "paper"} size="sm">
          {PLACEMENT_FORM_LABEL[placement.form]}
        </Pill>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          {price(placement.anchor_price, currency)}
        </span>
        {placement.distance_from_current_pct && (
          <span style={{ color: "var(--fg-2)" }}>
            ({pct(placement.distance_from_current_pct, 1)}
            {placement.within_policy_distance_band === false && " · 밴드 밖"})
          </span>
        )}
      </span>
      <span style={{ display: "flex", gap: 10, color: "var(--fg-2)" }}>
        {placement.quantity && <span>{quantity(placement.quantity)}</span>}
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          {money(placement.notional, currency)}
        </span>
        {placement.valid_until && <span>~{dateTime(placement.valid_until)}</span>}
      </span>
    </div>
  );
}

function SupportNetCard({ row }: Readonly<{ row: SupportNetRow }>) {
  return (
    <Card style={{ padding: 16, display: "grid", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <SymbolLink market={row.market} symbol={row.symbol} name={row.symbol_name} />
        <Pill tone={row.eligible ? "gain" : "paper"} size="sm">
          {row.eligible ? "이익권" : (row.ineligible_reason ?? "대상 아님")}
        </Pill>
      </div>
      <div
        style={{
          display: "grid",
          gap: 10,
          gridTemplateColumns: "repeat(auto-fit, minmax(96px, 1fr))",
        }}
      >
        <Field label="현재가" value={price(row.current_price, row.currency)} />
        <Field label="평단" value={price(row.average_price, row.currency)} />
        <Field label="평가손익" value={pct(row.unrealized_pnl_pct)} />
        <Field
          label="걸린 금액"
          value={money(row.placed_notional, row.currency)}
          title={exact(row.placed_notional) ?? "확인 불가"}
        />
        <Field
          label="남은 여력"
          value={money(row.remaining_headroom_notional, row.currency)}
          title={exact(row.remaining_headroom_notional) ?? "확인 불가"}
        />
      </div>
      {row.placements.length > 0 ? (
        <div style={{ display: "grid", gap: 4 }}>{row.placements.map((placement) => (
          <PlacementLine
            key={`${placement.form}:${placement.reference}`}
            placement={placement}
            currency={row.currency}
          />
        ))}</div>
      ) : row.placements_state === "ok" ? (
        <span style={{ fontSize: 12, color: "var(--fg-2)" }}>걸린 그물 없음</span>
      ) : (
        <span style={{ fontSize: 12, color: "var(--warn)" }}>
          걸린 그물을 확인하지 못했습니다 — 없다는 뜻이 아닙니다
        </span>
      )}
    </Card>
  );
}

export function SupportNetBlock({ tier }: Readonly<{ tier: SupportNetTier }>) {
  return (
    <section style={{ display: "grid", gap: 12 }}>
      <SectionHeading
        title="② 지지 그물 티어"
        subtitle={`${tier.policy_key}${
          tier.review_date ? ` · 채점 ${tier.review_date}` : ""
        }`}
      />
      {!tier.enabled ? (
        <EmptyNote>
          이 시장 범위에서는 지지 그물 티어를 표시하지 않습니다.
        </EmptyNote>
      ) : (
        <>
          {tier.placements_state !== "ok" && (
            <Card soft style={{ padding: 12, display: "grid", gap: 4 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--warn)" }}>
                걸린 주문·워치 조회 {SOURCE_STATE_LABEL[tier.placements_state]} — 걸린
                금액과 남은 여력을 확정하지 못했습니다
              </div>
              <ul
                style={{
                  margin: 0,
                  paddingLeft: 16,
                  fontSize: 11,
                  color: "var(--fg-2)",
                }}
              >
                {tier.placements_incomplete_reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </Card>
          )}
          <Card soft style={{ padding: 14, display: "flex", gap: 20, flexWrap: "wrap" }}>
            <Field
              label="티어 상한"
              value={money(tier.tier_cap_notional, tier.currency)}
            />
            <Field
              label="코인당 상한"
              value={money(tier.per_symbol_cap_notional, tier.currency)}
            />
            <Field
              label="걸린 합계"
              value={money(tier.placed_notional, tier.currency)}
              title={exact(tier.placed_notional) ?? "확인 불가"}
            />
            <Field
              label="티어 잔여"
              value={money(tier.remaining_notional, tier.currency)}
              title={exact(tier.remaining_notional) ?? "확인 불가"}
            />
            {tier.distance_band_pct.length === 2 && (
              <Field
                label="지지 거리 밴드"
                value={`${tier.distance_band_pct[0]}% ~ ${tier.distance_band_pct[1]}%`}
              />
            )}
          </Card>
          {tier.rows.length === 0 ? (
            <EmptyNote>해당 보유가 없습니다.</EmptyNote>
          ) : (
            <div style={{ display: "grid", gap: 10 }}>
              {tier.rows.map((row) => (
                <SupportNetCard key={row.symbol} row={row} />
              ))}
            </div>
          )}
          {tier.notes.length > 0 && (
            <ul
              style={{
                margin: 0,
                paddingLeft: 16,
                fontSize: 11,
                color: "var(--fg-2)",
                display: "grid",
                gap: 2,
              }}
            >
              {tier.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

export function ActiveWatchBlock({
  rows,
  approval,
}: Readonly<{ rows: ActiveBuyWatchRow[]; approval: ApprovalContext }>) {
  return (
    <section style={{ display: "grid", gap: 12 }}>
      <SectionHeading
        title="③ 활성 매수 워치"
        subtitle="발화하면 현금이 필요해지는 레벨. 금액은 워치의 max_action에서 옵니다."
      />
      {rows.length === 0 ? (
        <EmptyNote>활성 매수 워치가 없습니다.</EmptyNote>
      ) : (
        <div style={{ display: "grid", gap: 8 }}>
          {rows.map((row) => (
            <Card key={row.alert_uuid} style={{ padding: 14, display: "grid", gap: 8 }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  flexWrap: "wrap",
                }}
              >
                <SymbolLink
                  market={row.market}
                  symbol={row.symbol}
                  name={row.symbol_name}
                />
                <LaneBadge
                  lane={row.approval_lane}
                  reason={row.approval_lane_reason}
                  approval={approval}
                />
                <span title={row.account_mode ?? "max_action에 account_mode가 없습니다"}>
                  <Pill
                    tone={row.funding_broker === "unattributed" ? "warn" : "paper"}
                    size="sm"
                  >
                    {FUNDING_BROKER_LABEL[row.funding_broker]}
                  </Pill>
                </span>
                {row.near_expiry && (
                  <Pill tone="warn" size="sm">만료 임박</Pill>
                )}
              </div>
              <div
                style={{
                  display: "grid",
                  gap: 10,
                  gridTemplateColumns: "repeat(auto-fit, minmax(96px, 1fr))",
                }}
              >
                <Field
                  label={`레벨 (${row.metric})`}
                  value={price(row.threshold, row.currency)}
                  title={exact(row.threshold)}
                />
                <Field label="현재가" value={price(row.current_price, row.currency)} />
                <Field label="레벨까지" value={pct(row.distance_to_threshold_pct)} />
                <Field
                  label="예상 소요액"
                  value={money(row.planned_notional, row.currency)}
                  title={
                    exact(row.planned_notional)
                      ? `${row.planned_notional} (${row.planned_notional_source})`
                      : "max_action에 금액이 없습니다"
                  }
                />
                <Field label="만료" value={dateTime(row.valid_until)} />
              </div>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

export function DiscoveryGateBlock({
  rows,
}: Readonly<{ rows: DiscoveryGateRow[] }>) {
  if (rows.length === 0) return null;
  return (
    <section style={{ display: "grid", gap: 12 }}>
      <SectionHeading
        title="④ 발굴 게이트"
        subtitle="신규 후보를 볼 수 있는 국면인지. 확인 불가 조건은 충족으로 세지 않습니다."
      />
      {rows.map((gate) => (
        <Card key={gate.gate_key} style={{ padding: 16, display: "grid", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <Pill
              tone={
                gate.state === "open"
                  ? "gain"
                  : gate.state === "closed"
                    ? "loss"
                    : "warn"
              }
            >
              {GATE_STATE_LABEL[gate.state]}
            </Pill>
            <span style={{ fontSize: 12, color: "var(--fg-2)" }}>
              {gate.met_count}/{gate.of} 충족 (필요 {gate.min_conditions_met})
              {gate.unavailable_count > 0 && ` · 확인 불가 ${gate.unavailable_count}`}
            </span>
          </div>
          <div style={{ display: "grid", gap: 6 }}>
            {gate.conditions.map((condition) => (
              <div
                key={condition.condition_id}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: 10,
                  flexWrap: "wrap",
                  fontSize: 12,
                }}
                title={condition.note ?? undefined}
              >
                <span>
                  <Pill
                    tone={
                      condition.state === "met"
                        ? "gain"
                        : condition.state === "not_met"
                          ? "loss"
                          : "warn"
                    }
                    size="sm"
                  >
                    {GATE_CONDITION_STATE_LABEL[condition.state]}
                  </Pill>{" "}
                  {condition.metric}
                </span>
                <span style={{ fontVariantNumeric: "tabular-nums" }}>
                  {condition.current_value ?? "확인 불가"}
                  {condition.unit === "percent" && condition.current_value ? "%" : ""}
                  <span style={{ color: "var(--fg-2)" }}>
                    {" "}
                    (기준 {COMPARISON_LABEL[condition.comparison ?? ""] ?? condition.comparison}{" "}
                    {condition.threshold})
                  </span>
                </span>
              </div>
            ))}
          </div>
          {gate.notes.length > 0 && (
            <ul
              style={{
                margin: 0,
                paddingLeft: 16,
                fontSize: 11,
                color: "var(--fg-2)",
                display: "grid",
                gap: 2,
              }}
            >
              {gate.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}
        </Card>
      ))}
    </section>
  );
}

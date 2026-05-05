import type { ReconciliationPayload } from "../api/reconciliation";
import { formatDateTime } from "../format/datetime";
import { formatDecimal } from "../format/decimal";
import { formatPercent } from "../format/percent";
import { labelOrderSide } from "../i18n/formatters";
import styles from "./ReconciliationDecisionSupportPanel.module.css";

interface Props {
  side: string;
  originalPrice: string | null;
  originalQuantity: string | null;
  payload: ReconciliationPayload | null;
}

export default function ReconciliationDecisionSupportPanel({
  side,
  originalPrice,
  originalQuantity,
  payload,
}: Props) {
  if (payload === null) return null;
  const ds = payload.decision_support;
  return (
    <section
      aria-label="조정 의사결정 지원"
      className={styles.panel}
    >
      <dl className={styles.list}>
        <Item label="대기 방향" value={labelOrderSide(side)} />
        <Item label="대기 가격" value={formatDecimal(originalPrice)} />
        <Item label="대기 수량" value={formatDecimal(originalQuantity)} />
        <Item label="대기 주문" value={payload.pending_order_id ?? "—"} />
        <Item
          label="실시간 시세"
          value={
            payload.live_quote === null
              ? "—"
              : `${formatDecimal(payload.live_quote.price)} (${formatDateTime(
                  payload.live_quote.as_of,
                )})`
          }
        />
        <Item label="현재가 대비 괴리" value={formatPercent(ds.gap_pct)} />
        <Item
          label="체결까지 거리"
          value={formatPercent(ds.signed_distance_to_fill)}
        />
        <Item
          label="가까운 지지선"
          value={
            ds.nearest_support_price === null
              ? "—"
              : `${formatDecimal(ds.nearest_support_price)} (${formatPercent(
                  ds.nearest_support_distance_pct,
                )})`
          }
        />
        <Item
          label="가까운 저항선"
          value={
            ds.nearest_resistance_price === null
              ? "—"
              : `${formatDecimal(ds.nearest_resistance_price)} (${formatPercent(
                  ds.nearest_resistance_distance_pct,
                )})`
          }
        />
        <Item
          label="매수/매도 스프레드"
          value={formatPercent(ds.bid_ask_spread_pct)}
        />
      </dl>
    </section>
  );
}

function Item({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.item}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

import type { ReconciliationPayload } from "../api/reconciliation";
import { formatDateTime } from "../format/datetime";
import { formatDecimal } from "../format/decimal";
import { formatPercent } from "../format/percent";
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
      aria-label="Reconciliation decision support"
      className={styles.panel}
    >
      <dl className={styles.list}>
        <Item label="Pending side" value={side} />
        <Item label="Pending price" value={formatDecimal(originalPrice)} />
        <Item label="Pending qty" value={formatDecimal(originalQuantity)} />
        <Item label="Pending order" value={payload.pending_order_id ?? "—"} />
        <Item
          label="Live quote"
          value={
            payload.live_quote === null
              ? "—"
              : `${formatDecimal(payload.live_quote.price)} (${formatDateTime(
                  payload.live_quote.as_of,
                )})`
          }
        />
        <Item label="Gap to current" value={formatPercent(ds.gap_pct)} />
        <Item
          label="Distance to fill"
          value={formatPercent(ds.signed_distance_to_fill)}
        />
        <Item
          label="Nearest support"
          value={
            ds.nearest_support_price === null
              ? "—"
              : `${formatDecimal(ds.nearest_support_price)} (${formatPercent(
                  ds.nearest_support_distance_pct,
                )})`
          }
        />
        <Item
          label="Nearest resistance"
          value={
            ds.nearest_resistance_price === null
              ? "—"
              : `${formatDecimal(ds.nearest_resistance_price)} (${formatPercent(
                  ds.nearest_resistance_distance_pct,
                )})`
          }
        />
        <Item
          label="Bid/ask spread"
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

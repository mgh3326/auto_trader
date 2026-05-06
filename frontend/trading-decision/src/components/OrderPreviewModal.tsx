import React, { useState, useEffect } from "react";
import styles from "./OrderPreviewModal.module.css";
import type { OrderPreviewSession, CreateOrderPreviewRequest } from "../api/types";
import { orderPreviewsApi } from "../api/orderPreviews";

interface Props {
  isOpen: boolean;
  onClose: () => void;
  initialRequest?: CreateOrderPreviewRequest;
  previewUuid?: string;
  onSuccess?: (session: OrderPreviewSession) => void;
}

export const OrderPreviewModal: React.FC<Props> = ({
  isOpen,
  onClose,
  initialRequest,
  previewUuid: initialUuid,
  onSuccess,
}) => {
  const [session, setSession] = useState<OrderPreviewSession | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      if (initialUuid) {
        fetchSession(initialUuid);
      } else if (initialRequest) {
        createPreview(initialRequest);
      }
    } else {
      setSession(null);
      setError(null);
    }
  }, [isOpen, initialUuid, initialRequest]);

  const createPreview = async (req: CreateOrderPreviewRequest) => {
    setLoading(true);
    setError(null);
    try {
      const data = await orderPreviewsApi.create(req);
      setSession(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const fetchSession = async (uuid: string) => {
    setLoading(true);
    setError(null);
    try {
      const data = await orderPreviewsApi.get(uuid);
      setSession(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleRefresh = async () => {
    if (!session) return;
    setLoading(true);
    try {
      const data = await orderPreviewsApi.refresh(session.preview_uuid);
      setSession(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async () => {
    if (!session || !session.preview_uuid || !session.approval_token) return;
    
    setSubmitting(true);
    try {
      const data = await orderPreviewsApi.submit(session.preview_uuid, session.approval_token);
      setSession(data);
      if (onSuccess) onSuccess(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
        <div className={styles.header}>
          <h2>Order Preview</h2>
          <button className={styles.closeButton} onClick={onClose}>&times;</button>
        </div>

        <div className={styles.content}>
          {loading && <div className={styles.loading}>Loading preview...</div>}
          {error && <div className={styles.errorBox}>{error}</div>}

          {session && (
            <>
              <div className={styles.summary}>
                <div className={styles.summaryItem}>
                  <span className={styles.summaryLabel}>Symbol</span>
                  <span className={styles.summaryValue}>{session.symbol}</span>
                </div>
                <div className={styles.summaryItem}>
                  <span className={styles.summaryLabel}>Side</span>
                  <span className={`${styles.summaryValue} ${session.side === "buy" ? styles.side_buy : styles.side_sell}`}>
                    {session.side.toUpperCase()}
                  </span>
                </div>
                <div className={styles.summaryItem}>
                  <span className={styles.summaryLabel}>Status</span>
                  <span className={styles.summaryValue}>{session.status}</span>
                </div>
              </div>

              <div className={styles.legList}>
                {session.legs.map((leg) => (
                  <div key={leg.leg_index} className={styles.legItem}>
                    <div className={styles.legHeader}>
                      <span>Leg #{leg.leg_index + 1}</span>
                      <span>{leg.order_type.toUpperCase()}</span>
                    </div>
                    <div className={styles.legDetails}>
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Qty:</span>
                        <span className={styles.detailValue}>{leg.quantity}</span>
                      </div>
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Price:</span>
                        <span className={styles.detailValue}>{leg.price || "Market"}</span>
                      </div>
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Est. Value:</span>
                        <span className={styles.detailValue}>{leg.estimated_value || "---"}</span>
                      </div>
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Est. Fee:</span>
                        <span className={styles.detailValue}>{leg.estimated_fee || "---"}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <div className={styles.footer}>
          <button className={`${styles.btn} ${styles.btnCancel}`} onClick={onClose}>
            Cancel
          </button>
          {session?.status === "preview_passed" && (
            <button 
              className={`${styles.btn} ${styles.btnSubmit}`} 
              onClick={handleSubmit}
              disabled={submitting || loading}
            >
              {submitting ? "Submitting..." : "Confirm & Submit"}
            </button>
          )}
          {session?.status === "preview_failed" && (
            <button className={`${styles.btn} ${styles.btnSubmit}`} onClick={handleRefresh} disabled={loading}>
              Retry Preview
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

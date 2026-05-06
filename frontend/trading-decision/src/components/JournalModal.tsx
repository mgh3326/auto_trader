import React, { useState } from "react";
import { tradeJournal, COMMON } from "../i18n";
import styles from "./JournalModal.module.css";

interface JournalModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (data: any) => Promise<void>;
  initialData?: any;
}

export const JournalModal: React.FC<JournalModalProps> = ({
  isOpen,
  onClose,
  onSave,
  initialData,
}) => {
  const [thesis, setThesis] = useState(initialData?.thesis || "");
  const [strategy, setStrategy] = useState(initialData?.strategy || "");
  const [targetPrice, setTargetPrice] = useState(initialData?.target_price || "");
  const [stopLoss, setStopLoss] = useState(initialData?.stop_loss || "");
  const [minHoldDays, setMinHoldDays] = useState(initialData?.min_hold_days || "");
  const [status, setStatus] = useState(initialData?.status || "draft");
  const [notes, setNotes] = useState(initialData?.notes || "");
  const [saving, setSaving] = useState(false);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    try {
      await onSave({
        thesis,
        strategy: strategy || null,
        target_price: targetPrice ? parseFloat(targetPrice) : null,
        stop_loss: stopLoss ? parseFloat(stopLoss) : null,
        min_hold_days: minHoldDays ? parseInt(minHoldDays, 10) : null,
        status,
        notes: notes || null,
      });
      onClose();
    } catch (err) {
      console.error(err);
      alert(tradeJournal.saveError);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.overlay}>
      <div className={styles.modal}>
        <div className={styles.header}>
          <h2>{initialData?.id ? tradeJournal.modalTitleEdit : tradeJournal.modalTitleCreate}</h2>
          <button className={styles.closeBtn} onClick={onClose}>&times;</button>
        </div>
        <form onSubmit={handleSubmit} className={styles.form}>
          <div className={styles.field}>
            <label>{tradeJournal.labelThesis}</label>
            <textarea
              required
              value={thesis}
              onChange={(e) => setThesis(e.target.value)}
              placeholder={tradeJournal.placeholderThesis}
              rows={4}
            />
          </div>
          <div className={styles.grid}>
            <div className={styles.field}>
              <label>{tradeJournal.labelStrategy}</label>
              <input value={strategy} onChange={(e) => setStrategy(e.target.value)} />
            </div>
            <div className={styles.field}>
              <label>{tradeJournal.labelStatus}</label>
              <select value={status} onChange={(e) => setStatus(e.target.value as any)}>
                <option value="draft">Draft</option>
                <option value="active">Active</option>
              </select>
            </div>
            <div className={styles.field}>
              <label>{tradeJournal.labelTargetPrice}</label>
              <input type="number" step="any" value={targetPrice} onChange={(e) => setTargetPrice(e.target.value)} />
            </div>
            <div className={styles.field}>
              <label>{tradeJournal.labelStopLoss}</label>
              <input type="number" step="any" value={stopLoss} onChange={(e) => setStopLoss(e.target.value)} />
            </div>
            <div className={styles.field}>
              <label>{tradeJournal.labelMinHoldDays}</label>
              <input type="number" value={minHoldDays} onChange={(e) => setMinHoldDays(e.target.value)} />
            </div>
          </div>
          <div className={styles.field}>
            <label>{tradeJournal.labelNotes}</label>
            <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} />
          </div>
          <div className={styles.actions}>
            <button type="button" onClick={onClose} disabled={saving}>{COMMON.cancel}</button>
            <button type="submit" className={styles.primaryBtn} disabled={saving}>
              {saving ? COMMON.saving : tradeJournal.btnSave}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

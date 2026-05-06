import React, { useState } from "react";

import { tradeJournal, COMMON } from "../i18n";
import type {
  JournalCoverageRow,
  JournalCreateRequest,
  JournalUpdateRequest,
  WritableJournalStatus,
} from "../api/types";
import styles from "./JournalModal.module.css";

export type JournalModalSubmitPayload =
  | JournalCreateRequest
  | JournalUpdateRequest;

interface JournalModalProps {
  isOpen: boolean;
  mode: "create" | "edit";
  symbol: string;
  instrumentType: string;
  initialRow: JournalCoverageRow | null;
  onClose: () => void;
  onSave: (data: JournalModalSubmitPayload) => Promise<void>;
}

function toFloat(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function toInt(v: string): number | null {
  if (v.trim() === "") return null;
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
}

export const JournalModal: React.FC<JournalModalProps> = ({
  isOpen,
  mode,
  symbol,
  instrumentType,
  initialRow,
  onClose,
  onSave,
}) => {
  const [thesis, setThesis] = useState(initialRow?.thesis ?? "");
  const [strategy, setStrategy] = useState("");
  const [targetPrice, setTargetPrice] = useState(
    initialRow?.target_price !== null && initialRow?.target_price !== undefined
      ? String(initialRow.target_price)
      : "",
  );
  const [stopLoss, setStopLoss] = useState(
    initialRow?.stop_loss !== null && initialRow?.stop_loss !== undefined
      ? String(initialRow.stop_loss)
      : "",
  );
  const [minHoldDays, setMinHoldDays] = useState(
    initialRow?.min_hold_days !== null &&
      initialRow?.min_hold_days !== undefined
      ? String(initialRow.min_hold_days)
      : "",
  );
  const [status, setStatus] = useState<WritableJournalStatus>(
    mode === "edit" ? "active" : "draft",
  );
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (thesis.trim() === "") {
      setError(tradeJournal.labelThesis);
      return;
    }
    setSaving(true);
    setError(null);

    const baseFields = {
      thesis: thesis.trim(),
      strategy: strategy.trim() === "" ? null : strategy.trim(),
      target_price: toFloat(targetPrice),
      stop_loss: toFloat(stopLoss),
      min_hold_days: toInt(minHoldDays),
      status,
      notes: notes.trim() === "" ? null : notes.trim(),
    };

    const payload: JournalModalSubmitPayload =
      mode === "create"
        ? {
            symbol,
            instrument_type: instrumentType,
            side: "buy",
            ...baseFields,
          }
        : { ...baseFields };

    try {
      await onSave(payload);
      onClose();
    } catch (err) {
      console.error(err);
      setError(tradeJournal.saveError);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.overlay}>
      <div className={styles.modal}>
        <div className={styles.header}>
          <h2>
            {mode === "edit"
              ? tradeJournal.modalTitleEdit
              : tradeJournal.modalTitleCreate}
          </h2>
          <button
            type="button"
            className={styles.closeBtn}
            onClick={onClose}
            aria-label="close"
          >
            &times;
          </button>
        </div>
        <form onSubmit={handleSubmit} className={styles.form}>
          <p className={styles.symbolLine}>{symbol}</p>
          <div className={styles.field}>
            <label htmlFor="journal-thesis">{tradeJournal.labelThesis}</label>
            <textarea
              id="journal-thesis"
              required
              value={thesis}
              onChange={(e) => setThesis(e.target.value)}
              placeholder={tradeJournal.placeholderThesis}
              rows={4}
            />
          </div>
          <div className={styles.grid}>
            <div className={styles.field}>
              <label htmlFor="journal-strategy">
                {tradeJournal.labelStrategy}
              </label>
              <input
                id="journal-strategy"
                value={strategy}
                onChange={(e) => setStrategy(e.target.value)}
              />
            </div>
            <div className={styles.field}>
              <label htmlFor="journal-status">{tradeJournal.labelStatus}</label>
              <select
                id="journal-status"
                value={status}
                onChange={(e) =>
                  setStatus(e.target.value as WritableJournalStatus)
                }
              >
                <option value="draft">Draft</option>
                <option value="active">Active</option>
              </select>
            </div>
            <div className={styles.field}>
              <label htmlFor="journal-target">
                {tradeJournal.labelTargetPrice}
              </label>
              <input
                id="journal-target"
                type="number"
                step="any"
                value={targetPrice}
                onChange={(e) => setTargetPrice(e.target.value)}
              />
            </div>
            <div className={styles.field}>
              <label htmlFor="journal-stop">{tradeJournal.labelStopLoss}</label>
              <input
                id="journal-stop"
                type="number"
                step="any"
                value={stopLoss}
                onChange={(e) => setStopLoss(e.target.value)}
              />
            </div>
            <div className={styles.field}>
              <label htmlFor="journal-min-hold">
                {tradeJournal.labelMinHoldDays}
              </label>
              <input
                id="journal-min-hold"
                type="number"
                min={0}
                max={3650}
                value={minHoldDays}
                onChange={(e) => setMinHoldDays(e.target.value)}
              />
            </div>
          </div>
          <div className={styles.field}>
            <label htmlFor="journal-notes">{tradeJournal.labelNotes}</label>
            <textarea
              id="journal-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
            />
          </div>
          {error ? <p className={styles.error}>{error}</p> : null}
          <div className={styles.actions}>
            <button type="button" onClick={onClose} disabled={saving}>
              {COMMON.cancel}
            </button>
            <button
              type="submit"
              className={styles.primaryBtn}
              disabled={saving}
            >
              {saving ? COMMON.saving : tradeJournal.btnSave}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

import React from "react";
import type { CommitteeJournalPlaceholder as JournalType } from "../api/types";

interface Props {
  journalPlaceholder: JournalType | null;
}

export const CommitteeJournalPlaceholder: React.FC<Props> = ({
  journalPlaceholder,
}) => {
  if (!journalPlaceholder) return null;

  const hasContent =
    journalPlaceholder.journal_uuid || journalPlaceholder.notes;
  if (!hasContent) return null;

  return (
    <div className="committee-journal-placeholder">
      <h3>기록 / 사후 검토</h3>
      {journalPlaceholder.journal_uuid && (
        <div className="journal-uuid">
          기록 UUID: <code>{journalPlaceholder.journal_uuid}</code>
        </div>
      )}
      {journalPlaceholder.notes && (
        <p className="journal-notes">{journalPlaceholder.notes}</p>
      )}
      <style>{`
        .committee-journal-placeholder {
          padding: 16px;
          background: #fdfdfe;
          border: 1px solid #dee2e6;
          border-radius: 4px;
          margin-bottom: 16px;
        }
        .journal-uuid {
          font-size: 0.9em;
          color: #495057;
          margin-bottom: 8px;
        }
        .journal-uuid code {
          background: #f8f9fa;
          padding: 2px 6px;
          border-radius: 3px;
        }
        .journal-notes {
          margin: 0;
          color: #495057;
        }
      `}</style>
    </div>
  );
};

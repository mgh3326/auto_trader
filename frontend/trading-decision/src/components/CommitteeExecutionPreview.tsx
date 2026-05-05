import React from "react";
import type { CommitteeExecutionPreview as ExecutionPreviewType } from "../api/types";

interface Props {
  executionPreview: ExecutionPreviewType | null;
}

export const CommitteeExecutionPreview: React.FC<Props> = ({ executionPreview }) => {
  if (!executionPreview) return null;

  return (
    <div className="committee-execution-preview">
      <h3>Execution Preview</h3>
      {executionPreview.is_blocked && (
        <div className="block-warning">
          <strong>BLOCKED:</strong> {executionPreview.block_reason}
        </div>
      )}
      {executionPreview.preview_payload && (
        <div className="payload-preview">
          <pre>{JSON.stringify(executionPreview.preview_payload, null, 2)}</pre>
        </div>
      )}
      <style>{`
        .committee-execution-preview {
          padding: 16px;
          background: #fff;
          border: 1px solid #dee2e6;
          border-radius: 4px;
          margin-bottom: 16px;
        }
        .block-warning {
          padding: 12px;
          background: #fff3cd;
          border: 1px solid #ffeeba;
          color: #856404;
          border-radius: 4px;
          margin-bottom: 12px;
        }
        .payload-preview {
          background: #f8f9fa;
          padding: 12px;
          border-radius: 4px;
          overflow-x: auto;
        }
        .payload-preview pre {
          margin: 0;
          font-size: 0.85em;
        }
      `}</style>
    </div>
  );
};

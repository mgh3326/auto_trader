import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { createSession, listSessions } from "../api/researchPipeline";
import type {
  ResearchInstrumentType,
  ResearchSessionListItem,
} from "../api/types";
import {
  RESEARCH_INSTRUMENT_TYPE_LABEL,
  RESEARCH_SESSION_STATUS_LABEL,
  SUMMARY_DECISION_LABEL,
} from "../i18n/ko";
import ErrorView from "../components/ErrorView";
import LoadingView from "../components/LoadingView";
import styles from "./ResearchHomePage.module.css";

const INSTRUMENT_OPTIONS: ResearchInstrumentType[] = [
  "equity_kr",
  "equity_us",
  "crypto",
];

interface ListState {
  status: "idle" | "loading" | "success" | "error";
  data: ResearchSessionListItem[];
  error: string | null;
}

export default function ResearchHomePage() {
  const navigate = useNavigate();
  const [list, setList] = useState<ListState>({
    status: "loading",
    data: [],
    error: null,
  });
  const [version, setVersion] = useState(0);

  const [symbol, setSymbol] = useState("");
  const [instrumentType, setInstrumentType] =
    useState<ResearchInstrumentType>("equity_kr");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setList((c) => ({ ...c, status: "loading", error: null }));
    listSessions({ limit: 20 })
      .then((data) => {
        if (controller.signal.aborted) return;
        setList({ status: "success", data, error: null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setList({
          status: "error",
          data: [],
          error:
            error instanceof ApiError
              ? error.detail
              : "Something went wrong. Try again.",
        });
      });
    return () => controller.abort();
  }, [version]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!symbol.trim()) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const result = await createSession({
        symbol: symbol.trim(),
        instrument_type: instrumentType,
        triggered_by: "user",
      });
      void navigate(`/research/sessions/${result.session_id}`);
    } catch (error) {
      setSubmitError(
        error instanceof ApiError
          ? error.detail
          : "Something went wrong. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <h1>리서치 세션</h1>

      <section aria-label="새 세션 시작" className={styles.startCard}>
        <h2>새 분석 시작</h2>
        <form onSubmit={onSubmit}>
          <label>
            심볼{" "}
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              required
              autoComplete="off"
            />
          </label>
          <label>
            종목 유형{" "}
            <select
              value={instrumentType}
              onChange={(e) =>
                setInstrumentType(e.target.value as ResearchInstrumentType)
              }
            >
              {INSTRUMENT_OPTIONS.map((opt) => (
                <option key={opt} value={opt}>
                  {RESEARCH_INSTRUMENT_TYPE_LABEL[opt]}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={submitting}>
            {submitting ? "시작 중…" : "세션 시작"}
          </button>
          {submitError && <p role="alert">{submitError}</p>}
        </form>
      </section>

      <section aria-label="최근 세션">
        <div className={styles.refreshRow}>
          <h2>최근 세션</h2>
          <button type="button" onClick={() => setVersion((v) => v + 1)}>
            새로고침
          </button>
        </div>
        {list.status === "loading" && <LoadingView />}
        {list.status === "error" && (
          <ErrorView message={list.error ?? "오류가 발생했습니다."} />
        )}
        {list.status === "success" && (
          <table>
            <thead>
              <tr>
                <th>세션 ID</th>
                <th>상태</th>
                <th>생성</th>
                <th>결정</th>
                <th>신뢰도</th>
              </tr>
            </thead>
            <tbody>
              {list.data.map((row) => {
                const statusLabel =
                  RESEARCH_SESSION_STATUS_LABEL[
                    row.status as keyof typeof RESEARCH_SESSION_STATUS_LABEL
                  ] ?? row.status;
                return (
                  <tr key={row.id} aria-label={String(row.id)}>
                    <td>
                      <Link to={`/research/sessions/${row.id}`}>{row.id}</Link>
                    </td>
                    <td>{statusLabel}</td>
                    <td>{row.created_at}</td>
                    <td>
                      {row.decision
                        ? SUMMARY_DECISION_LABEL[row.decision]
                        : "—"}
                    </td>
                    <td>{row.confidence != null ? `${row.confidence}%` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

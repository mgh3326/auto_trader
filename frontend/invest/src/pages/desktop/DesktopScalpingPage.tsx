// ROB-315 Phase 3 — /invest/scalping (스캘핑 일지).
// Read-only daily review surface for the Binance Demo scalping loop. No order
// placement, no scheduler toggle — it only builds/reads the review rollup and
// displays the day's round-trips so the operator can decide the next small
// improvement.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  buildScalpingDraft,
  fetchScalpingReview,
  fetchScalpingReviews,
  fetchScalpingTrades,
} from "../../api/scalping";
import { PageSafetyNote } from "../../components/PageSafetyNote";
import { DesktopShell } from "../../desktop/DesktopShell";
import { Button, Card } from "../../ds";
import type {
  ScalpingProduct,
  ScalpingReview,
  ScalpingReviewAction,
  ScalpingTrade,
} from "../../types/scalping";

const DECISION_LABEL: Record<string, string> = {
  review: "검토 필요",
  keep: "유지",
  adjust: "조정",
  pause: "일시중지",
  disable: "비활성화",
};

const STATUS_LABEL: Record<string, string> = {
  draft: "초안",
  reviewed: "검토됨",
  locked: "잠김",
};

const SESSION_TAG_LABEL: Record<string, string> = { "": "규칙", llm: "LLM" };
const sessionTagLabel = (tag: string): string => SESSION_TAG_LABEL[tag] ?? tag ?? "규칙";

const ACTION_STATUS_LABEL: Record<string, string> = {
  open: "열림",
  applied: "적용됨",
  skipped: "건너뜀",
  superseded: "대체됨",
};

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

// Render a nullable metric as "n/a" rather than 0/blank/fabricated (ROB-315:
// deferred telemetry may be null on legacy/anomaly rows).
function na(value: string | number | null | undefined): string {
  return value === null || value === undefined ? "n/a" : String(value);
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <div style={{ color: "var(--fg-3)", fontSize: 12 }}>{label}</div>
      <strong style={{ fontSize: 22 }}>{value}</strong>
    </Card>
  );
}

function LoopRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "96px 1fr", gap: 12, padding: "6px 0" }}>
      <span style={{ color: "var(--fg-3)", fontWeight: 700 }}>{label}</span>
      <span style={{ color: value ? "var(--fg)" : "var(--fg-3)" }}>{value ?? "기록 없음"}</span>
    </div>
  );
}

export function ScalpingRoute() {
  const [date, setDate] = useState<string>(today());
  const [product, setProduct] = useState<ScalpingProduct>("usdm_futures");
  const [review, setReview] = useState<ScalpingReview | null>(null);
  const [actions, setActions] = useState<ScalpingReviewAction[]>([]);
  const [trades, setTrades] = useState<ScalpingTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [building, setBuilding] = useState(false);
  const [reviewList, setReviewList] = useState<ScalpingReview[]>([]);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setErr(null);
    try {
      const [reviews, tradesResp] = await Promise.all([
        fetchScalpingReviews({ date, product, signal }),
        fetchScalpingTrades({ date, product, signal }),
      ]);
      setReviewList(reviews.items);
      const found = reviews.items[0] ?? null;
      setReview(found);
      setTrades(tradesResp.items);
      if (found) {
        const detail = await fetchScalpingReview(found.id, signal);
        setActions(detail.actions);
      } else {
        setActions([]);
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [date, product]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const onBuildDraft = useCallback(async () => {
    setBuilding(true);
    try {
      await buildScalpingDraft({ reviewDate: date, product });
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBuilding(false);
    }
  }, [date, product, load]);

  const metrics = review?.metrics;
  const isEmpty = !loading && !err && review === null && trades.length === 0;

  const exitReasonSummary = useMemo(() => {
    if (!metrics) return "";
    return Object.entries(metrics.exitReasonCounts)
      .map(([reason, count]) => `${reason} ${count}`)
      .join(" · ");
  }, [metrics]);

  return (
    <DesktopShell
      center={
        <div style={{ padding: "20px 28px", display: "grid", gap: 16 }}>
          <PageSafetyNote
            routeId="scalping"
            heading="ROB-315 스캘핑 일지 — demo 전용"
            tag="스캘핑"
            items={[
              "Binance Demo 전용 (demo-api / demo-fapi). live/mainnet 거래 없음",
              "이 페이지에서 주문·스케줄러 토글·파라미터 자동 적용 없음",
              "리뷰 액션은 운영자 기록일 뿐 자동 실행되지 않음",
              "원시 분석 행은 편집 불가 — 판단/액션만 리뷰 레이어에 기록",
            ]}
          />

          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <h1 style={{ margin: 0, fontSize: 22 }}>스캘핑 일지</h1>
            <input
              type="date"
              aria-label="review date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              style={{ padding: "6px 8px", borderRadius: 8, border: "1px solid var(--divider)", background: "var(--surface)", color: "var(--fg)" }}
            />
            <select
              aria-label="product"
              value={product}
              onChange={(e) => setProduct(e.target.value as ScalpingProduct)}
              style={{ padding: "6px 8px", borderRadius: 8, border: "1px solid var(--divider)", background: "var(--surface)", color: "var(--fg)" }}
            >
              <option value="usdm_futures">USD-M Futures</option>
              <option value="spot">Spot</option>
            </select>
            <Button onClick={onBuildDraft} disabled={building}>
              {building ? "생성 중…" : review ? "초안 새로고침" : "초안 만들기"}
            </Button>
          </div>

          {loading && <Card>로딩 중…</Card>}
          {err && (
            <Card>
              <span style={{ color: "#dc2626" }}>스캘핑 API 오류: {err}</span>
            </Card>
          )}

          {isEmpty && (
            <Card>
              <div style={{ display: "grid", gap: 6 }}>
                <strong>아직 분석 데이터가 없습니다</strong>
                <span style={{ color: "var(--fg-3)" }}>
                  일일 리뷰는 해당 날짜·상품의 <code>scalp_trade_analytics</code> 라운드트립이 있어야
                  의미가 있습니다. Demo 스캘핑 실행 후 다시 확인하거나, 데이터가 있으면 "초안 만들기"를 누르세요.
                </span>
              </div>
            </Card>
          )}

          {!loading && !err && !isEmpty && (
            <>
              {/* 1. Today summary */}
              {metrics ? (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 12 }}>
                  <MetricCard label="라운드트립" value={String(metrics.tradeCount)} />
                  <MetricCard label="승/패" value={`${metrics.winCount} / ${metrics.lossCount}`} />
                  <MetricCard label="순손익 (USDT)" value={na(metrics.netPnlUsdt)} />
                  <MetricCard label="이상치" value={String(metrics.anomalyCount)} />
                  <MetricCard label="결정" value={DECISION_LABEL[review!.decision] ?? review!.decision} />
                </div>
              ) : (
                <Card>
                  분석 행 {trades.length}건이 있지만 아직 리뷰 초안이 없습니다. "초안 만들기"로 롤업을 생성하세요.
                </Card>
              )}

              {/* 1.5 Per-session_tag comparison (LLM vs rule baseline) */}
              {reviewList.length > 1 && (
                <Card>
                  <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>세션별 비교 (LLM vs 규칙)</h2>
                  <div data-testid="scalping-session-comparison" style={{ display: "grid", gap: 6 }}>
                    {reviewList.map((r) => (
                      <div key={r.id} style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: 13 }}>
                        <strong style={{ minWidth: 48 }}>{sessionTagLabel(r.sessionTag)}</strong>
                        <span style={{ color: "var(--fg-3)" }}>{r.metrics.tradeCount}건</span>
                        <span style={{ color: "var(--fg-3)" }}>승/패 {r.metrics.winCount}/{r.metrics.lossCount}</span>
                        <span>net {na(r.metrics.netReturnBps)} bps</span>
                        <span>순손익 {na(r.metrics.netPnlUsdt)}</span>
                      </div>
                    ))}
                  </div>
                </Card>
              )}

              {/* 2. Daily loop card */}
              {review && (
                <Card>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                    <h2 style={{ margin: 0, fontSize: 16 }}>실행 → 관측 → 원인 → 개선 → 다음 실행</h2>
                    <span style={{ color: "var(--fg-3)", fontSize: 12 }}>
                      [{sessionTagLabel(review.sessionTag)}] {STATUS_LABEL[review.status] ?? review.status}
                      {exitReasonSummary ? ` · 종료사유: ${exitReasonSummary}` : ""}
                    </span>
                  </div>
                  <LoopRow label="관측" value={review.observation} />
                  <LoopRow label="원인" value={review.rootCause} />
                  <LoopRow label="개선" value={review.improvement} />
                  <LoopRow label="다음 실행" value={review.nextRunPlan} />
                </Card>
              )}

              {/* 3. Trade analytics table */}
              <Card>
                <h2 style={{ margin: "0 0 12px", fontSize: 16 }}>거래 분석</h2>
                {trades.length === 0 ? (
                  <span style={{ color: "var(--fg-3)" }}>해당 날짜의 라운드트립이 없습니다.</span>
                ) : (
                  <div style={{ overflowX: "auto" }}>
                    <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 900 }}>
                      <thead>
                        <tr style={{ textAlign: "left", color: "var(--fg-3)", fontSize: 12 }}>
                          {["심볼", "방향", "진입", "청산", "슬리피지", "스프레드", "MAE/MFE", "순손익", "종료", "보유(s)"].map((h) => (
                            <th key={h} style={{ padding: "0 10px 8px" }}>{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {trades.map((t) => (
                          <tr
                            key={t.id}
                            style={{ borderTop: "1px solid var(--divider)", fontSize: 13, background: t.isAnomaly ? "rgba(220,38,38,0.06)" : undefined }}
                          >
                            <td style={{ padding: "8px 10px" }}>
                              {t.symbol}
                              {t.isAnomaly && <span title="체결가 미확보" style={{ marginLeft: 6, color: "#dc2626", fontSize: 11 }}>이상치</span>}
                            </td>
                            <td style={{ padding: "8px 10px" }}>{t.side}</td>
                            <td style={{ padding: "8px 10px" }}>{na(t.entryPrice)}</td>
                            <td style={{ padding: "8px 10px" }}>{na(t.exitPrice)}</td>
                            <td style={{ padding: "8px 10px" }}>{na(t.entrySlippageBps)}</td>
                            <td style={{ padding: "8px 10px" }}>{na(t.entrySpreadBps)}</td>
                            <td style={{ padding: "8px 10px" }}>{na(t.maeBps)} / {na(t.mfeBps)}</td>
                            <td style={{ padding: "8px 10px" }}>{na(t.netPnlUsdt)}</td>
                            <td style={{ padding: "8px 10px" }}>{t.exitReason ?? "n/a"}</td>
                            <td style={{ padding: "8px 10px" }}>{na(t.holdingSeconds)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>

              {/* 4. Review actions */}
              {review && (
                <Card>
                  <h2 style={{ margin: "0 0 12px", fontSize: 16 }}>리뷰 액션</h2>
                  {actions.length === 0 ? (
                    <span style={{ color: "var(--fg-3)" }}>등록된 액션이 없습니다.</span>
                  ) : (
                    <div style={{ display: "grid", gap: 8 }}>
                      {actions.map((a) => (
                        <div
                          key={a.id}
                          style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap", borderTop: "1px solid var(--divider)", paddingTop: 8 }}
                        >
                          <span style={{ fontWeight: 700 }}>{a.title}</span>
                          <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{a.actionType}</span>
                          <span style={{ fontSize: 12, fontWeight: 700, color: a.status === "applied" ? "#16a34a" : "var(--fg-3)" }}>
                            {ACTION_STATUS_LABEL[a.status] ?? a.status}
                          </span>
                          {a.rationale && <span style={{ color: "var(--fg-3)", fontSize: 12 }}>· {a.rationale}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </Card>
              )}

              {/* 5. Safety panel */}
              <Card>
                <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>안전 상태</h2>
                <ul style={{ margin: 0, paddingLeft: 18, color: "var(--fg-3)", fontSize: 13, display: "grid", gap: 4 }}>
                  <li>실행 venue: Demo 전용 (demo-api.binance.com / demo-fapi.binance.com)</li>
                  <li>계정 범위: {review?.accountScope ?? "binance_demo"} (KIS/Upbit live와 분리)</li>
                  <li>이 페이지에서 주문/스케줄러 변경 없음 · 리뷰 액션은 운영자 기록</li>
                </ul>
              </Card>
            </>
          )}
        </div>
      }
    />
  );
}

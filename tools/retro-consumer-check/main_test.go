package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

// Synthetic retro_trades envelope: one closed trade with one linked review.
// Values are fixture-only (symbol 005930 is already used by the other gate
// fixtures); decimals are exact strings as the contract requires.
const retroTradesSyntheticBody = `{
  "as_of": "2026-01-10T00:00:00+00:00",
  "data_as_of": "2026-01-05T06:00:00+00:00",
  "timezone": "KST",
  "total": 1,
  "truncated": false,
  "limit": 50,
  "cursor": null,
  "next_cursor": null,
  "unknowns": [],
  "data": [{
    "trade_ref": "trade-0123456789abcdef0123",
    "symbol": "005930",
    "market": "kr",
    "side": "buy",
    "strategy": "S_ALPHA",
    "entry_at": "2026-01-02T00:00:00+00:00",
    "exit_at": "2026-01-05T06:00:00+00:00",
    "quantity": "3",
    "entry_price": "70000",
    "exit_price": "72000",
    "currency": "KRW",
    "return_rate": "2.8571",
    "realized_amount": "6000",
    "realized_currency": "KRW",
    "exit_reason": "take_profit",
    "basis": "inference",
    "reviews": [{
      "review_ref": "review-0123456789abcdef0123",
      "outcome": "filled",
      "trigger_type": "take_profit",
      "root_cause_class": "none",
      "pnl_status": "recorded",
      "pnl_class": "recorded",
      "realized_pnl": "6000",
      "realized_pnl_currency": "KRW",
      "fill_evidence_available": true,
      "created_at": "2026-01-05T06:00:00+00:00",
      "lesson": "synthetic lesson",
      "result_summary": "synthetic result",
      "rationale": "synthetic rationale",
      "next_strategy": "synthetic next"
    }]
  }]
}`

func retroTradesDecoded(t *testing.T) map[string]any {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader([]byte(retroTradesSyntheticBody)))
	dec.UseNumber()
	var raw map[string]any
	if err := dec.Decode(&raw); err != nil {
		t.Fatalf("decode synthetic envelope: %v", err)
	}
	return raw
}

func firstReview(t *testing.T, raw map[string]any) map[string]any {
	t.Helper()
	row := raw["data"].([]any)[0].(map[string]any)
	return row["reviews"].([]any)[0].(map[string]any)
}

func TestRetroTradesReviewAcceptsContractShape(t *testing.T) {
	counts, err := validateWireEnvelope(retroTradesDecoded(t), retroTradesOperation())
	if err != nil {
		t.Fatalf("contract-shaped review rejected: %v", err)
	}
	if counts.rows != 1 || counts.nested != 1 {
		t.Fatalf("checked rows=%d nested=%d, want 1 and 1", counts.rows, counts.nested)
	}
}

func TestRetroTradesReviewMissingRequiredKeyNamesField(t *testing.T) {
	// Mutant (a): one required key deleted from a review element.
	raw := retroTradesDecoded(t)
	delete(firstReview(t, raw), "fill_evidence_available")

	_, err := validateWireEnvelope(raw, retroTradesOperation())
	if err == nil {
		t.Fatal("review element without fill_evidence_available was accepted")
	}
	if !strings.Contains(err.Error(), `"fill_evidence_available"`) {
		t.Fatalf("error does not name the missing field: %v", err)
	}
}

func TestRetroTradesReviewExtraKeyNamesField(t *testing.T) {
	// Mutant (b): one key outside the closed review schema.
	raw := retroTradesDecoded(t)
	firstReview(t, raw)["unexpected_field"] = "synthetic"

	_, err := validateWireEnvelope(raw, retroTradesOperation())
	if err == nil {
		t.Fatal("review element with an extra key was accepted")
	}
	if !strings.Contains(err.Error(), `"unexpected_field"`) {
		t.Fatalf("error does not name the extra field: %v", err)
	}
}

func TestRetroTradesReviewDecimalMustStayString(t *testing.T) {
	raw := retroTradesDecoded(t)
	firstReview(t, raw)["realized_pnl"] = json.Number("6000")

	_, err := validateWireEnvelope(raw, retroTradesOperation())
	if err == nil {
		t.Fatal("numeric realized_pnl was accepted; decimals must stay strings")
	}
	if !strings.Contains(err.Error(), `"realized_pnl"`) {
		t.Fatalf("error does not name the decimal field: %v", err)
	}
}

func TestRetroTradesEmptyDataFailsCoverage(t *testing.T) {
	raw := retroTradesDecoded(t)
	raw["data"] = []any{}

	counts, err := validateWireEnvelope(raw, retroTradesOperation())
	if err != nil {
		t.Fatalf("empty data is a coverage failure, not a shape failure: %v", err)
	}
	if err := checkCoverage(retroTradesOperation(), counts); err == nil {
		t.Fatal("retro_trades with no checked row passed the coverage check")
	}
}

func TestRetroTradesReviewCodeFieldPatternNamesField(t *testing.T) {
	// The contract pins review_ref (and the other code fields) to ^[A-Za-z0-9_.:\-]+$.
	raw := retroTradesDecoded(t)
	firstReview(t, raw)["review_ref"] = "review with space"

	_, err := validateWireEnvelope(raw, retroTradesOperation())
	if err == nil {
		t.Fatal("review_ref with a space was accepted; the contract pattern forbids it")
	}
	if !strings.Contains(err.Error(), `"review_ref"`) {
		t.Fatalf("error does not name the code field: %v", err)
	}
}

func TestRetroTradesReviewCodeFieldMaxLengthNamesField(t *testing.T) {
	raw := retroTradesDecoded(t)
	firstReview(t, raw)["outcome"] = strings.Repeat("a", 65)

	_, err := validateWireEnvelope(raw, retroTradesOperation())
	if err == nil {
		t.Fatal("65-character outcome was accepted; the contract allows at most 64")
	}
	if !strings.Contains(err.Error(), `"outcome"`) {
		t.Fatalf("error does not name the code field: %v", err)
	}
}

func TestRetroTradesReviewProseMaxLengthNamesField(t *testing.T) {
	raw := retroTradesDecoded(t)
	firstReview(t, raw)["lesson"] = strings.Repeat("가", 1001)

	_, err := validateWireEnvelope(raw, retroTradesOperation())
	if err == nil {
		t.Fatal("1001-character lesson was accepted; the contract allows at most 1000")
	}
	if !strings.Contains(err.Error(), `"lesson"`) {
		t.Fatalf("error does not name the prose field: %v", err)
	}
}

// Synthetic retro_decisions envelope: one recorded proposal decision with every
// DecisionRow key present (45 keys, openapi components/DecisionRow). Values are
// fixture-only; decimals are exact strings and the nested objects are the closed
// TradeCosts and Excursions schemas.
const retroDecisionsExcursionDone = `{"amount": "1000", "pct": "1.4286", "r": "0.3333", "status": "complete", "reason": null}`
const retroDecisionsExcursionPending = `{"amount": null, "pct": null, "r": null, "status": "pending", "reason": "window_open"}`
const retroDecisionsExcursionUnknown = `{"amount": null, "pct": null, "r": null, "status": "unknown", "reason": "no_complete_session"}`

const retroDecisionsSyntheticBody = `{
  "as_of": "2026-01-10T00:00:00+00:00",
  "data_as_of": "2026-01-05T06:00:00+00:00",
  "timezone": "KST",
  "total": 1,
  "truncated": false,
  "limit": 100,
  "cursor": null,
  "next_cursor": null,
  "unknowns": [],
  "data": [{
    "decision_ref": "decision-0123456789abcdef0123",
    "decision_kind": "proposal",
    "decided_at": "2026-01-05T00:30:00+00:00",
    "decided_by": "order_proposals",
    "market": "kr",
    "symbol": "005930",
    "side": "buy",
    "strategy": "S_ALPHA",
    "candidate_ref": "candidate-0123456789abcdef0123",
    "candidate_link_basis": "recorded",
    "candidate_reason": null,
    "reference_price": "70000",
    "reference_kind": "limit",
    "reference_reason": null,
    "stop_price": "69000",
    "stop_source": "policy",
    "risk_basis": "recorded",
    "planned_quantity": "3",
    "planned_reason": null,
    "planned_risk_amount": "3000",
    "risk_reason": null,
    "risk_pct_of_notional": "1.4286",
    "risk_pct_of_budget": "0.3000",
    "risk_pct_of_budget_reason": null,
    "proposal_ref": "proposal-0123456789abcdef0123",
    "proposal_reason": null,
    "order_count": 1,
    "order_count_reason": null,
    "order_states": ["filled"],
    "order_states_reason": null,
    "entry_avg_price": "70000",
    "filled_quantity": "3",
    "fill_reason": null,
    "fill_time_basis": "broker_fill_time",
    "costs": {
      "recorded": {"fee": "150", "tax": "0", "currency": "KRW", "fee_includes_tax": false},
      "recorded_reason": null,
      "estimated": null,
      "estimated_reason": "missing_notional"
    },
    "mfe": {"1": ` + retroDecisionsExcursionDone + `, "5": ` + retroDecisionsExcursionPending + `, "20": ` + retroDecisionsExcursionUnknown + `, "to_exit": ` + retroDecisionsExcursionUnknown + `},
    "mae": {"1": ` + retroDecisionsExcursionDone + `, "5": ` + retroDecisionsExcursionPending + `, "20": ` + retroDecisionsExcursionUnknown + `, "to_exit": ` + retroDecisionsExcursionUnknown + `},
    "excursion_basis": "inference",
    "adjusted": null,
    "retrospective_ref": "review-0123456789abcdef0123",
    "retrospective_link_basis": "recorded",
    "retrospective_reason": null,
    "r_multiple": null,
    "r_multiple_reason": "no_exit_fill",
    "basis": "inference"
  }]
}`

func decodeWireBody(t *testing.T, body string) map[string]any {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader([]byte(body)))
	dec.UseNumber()
	var raw map[string]any
	if err := dec.Decode(&raw); err != nil {
		t.Fatalf("decode wire body: %v", err)
	}
	return raw
}

func retroDecisionsDecoded(t *testing.T) map[string]any {
	t.Helper()
	return decodeWireBody(t, retroDecisionsSyntheticBody)
}

func firstDecisionRow(t *testing.T, raw map[string]any) map[string]any {
	t.Helper()
	return raw["data"].([]any)[0].(map[string]any)
}

func TestRetroDecisionsAcceptsContractShape(t *testing.T) {
	op := retroDecisionsOperation()
	counts, err := validateWireEnvelope(retroDecisionsDecoded(t), op)
	if err != nil {
		t.Fatalf("contract-shaped decision row rejected: %v", err)
	}
	if counts.rows != 1 || counts.nested != 0 {
		t.Fatalf("checked rows=%d nested=%d, want 1 and 0", counts.rows, counts.nested)
	}
	if err := checkCoverage(op, counts); err != nil {
		t.Fatalf("contract-shaped fixture failed coverage: %v", err)
	}
}

func TestRetroDecisionsWireBytes(t *testing.T) {
	// Each case rewrites one token of the good bytes. The bad forms must be rejected
	// with an error that names the offending key; the replacement must actually occur.
	cases := []struct {
		name    string
		old     string
		new     string
		names   string
		wantErr bool
	}{
		{name: "good bytes", wantErr: false},
		{name: "decimal serialized as a JSON number", old: `"reference_price": "70000"`, new: `"reference_price": 70000`, names: `"reference_price"`, wantErr: true},
		{name: "integer serialized with a fraction", old: `"order_count": 1,`, new: `"order_count": 1.0,`, names: `"order_count"`, wantErr: true},
		{name: "negative order count", old: `"order_count": 1,`, new: `"order_count": -1,`, names: `"order_count"`, wantErr: true},
		{name: "market outside the closed enum", old: `"market": "kr"`, new: `"market": "jp"`, names: `"market"`, wantErr: true},
		{name: "ref with a space", old: `"decision_ref": "decision-0123456789abcdef0123"`, new: `"decision_ref": "decision ref"`, names: `"decision_ref"`, wantErr: true},
		{name: "boolean as a string", old: `"fee_includes_tax": false`, new: `"fee_includes_tax": "false"`, names: `"fee_includes_tax"`, wantErr: true},
		{name: "unknown top-level key", old: `"symbol": "005930",`, new: `"symbol": "005930",
    "account_no": "12345678-01",`, names: `"account_no"`, wantErr: true},
		{name: "excursion status outside the enum", old: `"status": "pending"`, new: `"status": "done"`, names: `"status"`, wantErr: true},
		{name: "adjusted must be boolean or null", old: `"adjusted": null`, new: `"adjusted": "no"`, names: `"adjusted"`, wantErr: true},
		{name: "order state outside the enum", old: `"order_states": ["filled"]`, new: `"order_states": ["done"]`, names: `"order_states"`, wantErr: true},
		{name: "reason outside the closed vocabulary", old: `"r_multiple_reason": "no_exit_fill"`, new: `"r_multiple_reason": "free text"`, names: `"r_multiple_reason"`, wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := retroDecisionsSyntheticBody
			if tc.old != "" {
				if !strings.Contains(body, tc.old) {
					t.Fatalf("fixture has no %q; the mutation would be vacuous", tc.old)
				}
				body = strings.Replace(body, tc.old, tc.new, 1)
			}
			_, err := validateWireEnvelope(decodeWireBody(t, body), retroDecisionsOperation())
			if !tc.wantErr {
				if err != nil {
					t.Fatalf("good bytes rejected: %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("bad bytes accepted: %s", tc.new)
			}
			if !strings.Contains(err.Error(), tc.names) {
				t.Fatalf("error does not name %s: %v", tc.names, err)
			}
		})
	}
}

func TestRetroDecisionsMissingRequiredKeyNamesField(t *testing.T) {
	raw := retroDecisionsDecoded(t)
	delete(firstDecisionRow(t, raw), "risk_pct_of_budget_reason")

	_, err := validateWireEnvelope(raw, retroDecisionsOperation())
	if err == nil {
		t.Fatal("decision row without risk_pct_of_budget_reason was accepted")
	}
	if !strings.Contains(err.Error(), `"risk_pct_of_budget_reason"`) {
		t.Fatalf("error does not name the missing field: %v", err)
	}
}

func TestRetroDecisionsOmittedExcursionHorizonNamesField(t *testing.T) {
	raw := retroDecisionsDecoded(t)
	delete(firstDecisionRow(t, raw)["mfe"].(map[string]any), "20")

	_, err := validateWireEnvelope(raw, retroDecisionsOperation())
	if err == nil {
		t.Fatal("mfe without the 20-session horizon was accepted")
	}
	if !strings.Contains(err.Error(), `"20"`) {
		t.Fatalf("error does not name the missing horizon: %v", err)
	}
}

func TestRetroDecisionsCostsExtraKeyNamesField(t *testing.T) {
	raw := retroDecisionsDecoded(t)
	recorded := firstDecisionRow(t, raw)["costs"].(map[string]any)["recorded"].(map[string]any)
	recorded["unexpected_field"] = "synthetic"

	_, err := validateWireEnvelope(raw, retroDecisionsOperation())
	if err == nil {
		t.Fatal("costs.recorded with an extra key was accepted")
	}
	if !strings.Contains(err.Error(), `"unexpected_field"`) {
		t.Fatalf("error does not name the extra field: %v", err)
	}
}

func TestRetroDecisionsOptionalExcursionReasonAccepted(t *testing.T) {
	raw := retroDecisionsDecoded(t)
	firstDecisionRow(t, raw)["mfe"].(map[string]any)["5"].(map[string]any)["r_reason"] = "no_complete_session"

	if _, err := validateWireEnvelope(raw, retroDecisionsOperation()); err != nil {
		t.Fatalf("excursion with the optional r_reason rejected: %v", err)
	}
}

func TestRetroDecisionsRefMaxLengthNamesField(t *testing.T) {
	raw := retroDecisionsDecoded(t)
	firstDecisionRow(t, raw)["decision_ref"] = strings.Repeat("a", 129)

	_, err := validateWireEnvelope(raw, retroDecisionsOperation())
	if err == nil {
		t.Fatal("129-character decision_ref was accepted; the contract allows at most 128")
	}
	if !strings.Contains(err.Error(), `"decision_ref"`) {
		t.Fatalf("error does not name the ref field: %v", err)
	}
}

func TestRetroDecisionsEmptyDataFailsCoverage(t *testing.T) {
	raw := retroDecisionsDecoded(t)
	raw["data"] = []any{}

	op := retroDecisionsOperation()
	counts, err := validateWireEnvelope(raw, op)
	if err != nil {
		t.Fatalf("empty data is a coverage failure, not a shape failure: %v", err)
	}
	if err := checkCoverage(op, counts); err == nil {
		t.Fatal("retro_decisions with no checked row passed the coverage check")
	}
}

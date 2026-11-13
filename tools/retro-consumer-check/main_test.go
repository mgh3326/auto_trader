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

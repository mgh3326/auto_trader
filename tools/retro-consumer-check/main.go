// Package main implements the Go contract gate harness for auto_trader retro/v1.
// It uses the pinned factortrail Go consumer client to invoke all nine retro/v1
// operations on a candidate provider and asserts schema, authorization, privacy,
// precision, and unknown/gap contract invariants.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/mgh3326/factortrail/internal/providers/autotrader"
)

var (
	accountNoPattern = regexp.MustCompile(`\b\d{8}-\d{2}\b`)
	brokerOrdPattern = regexp.MustCompile(`\b(?:ord_|broker_ord_)[0-9a-zA-Z]+\b`)
	tracebackPattern = regexp.MustCompile(`Traceback \(most recent call last\)|InternalServerError`)
	cursorPattern    = regexp.MustCompile(`^[A-Za-z0-9_.:\-]{1,128}$`)
	scoreCursorPat   = regexp.MustCompile(`^o[0-9]{1,11}$`)
)

type lockFileEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type lockedContract struct {
	Name               string          `json:"name"`
	SourceRepo         string          `json:"source_repo"`
	SourceCommit       string          `json:"source_commit"`
	SourcePath         string          `json:"source_path"`
	Path               string          `json:"path"`
	SHA256             string          `json:"sha256"`
	ExamplesSourcePath string          `json:"examples_source_path"`
	Examples           []lockFileEntry `json:"examples"`
}

type consumerLock struct {
	Version   int              `json:"version"`
	Contracts []lockedContract `json:"contracts"`
}

func parseDate(s string) openapi_types.Date {
	t, err := time.Parse(time.DateOnly, s)
	if err != nil {
		panic(fmt.Sprintf("invalid date %q: %v", s, err))
	}
	return openapi_types.Date{Time: t}
}

func fileSHA256(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}

func verifyContractLock(consumerLockPath, candidateSpec, candidateExamplesDir string) (bool, error) {
	raw, err := os.ReadFile(consumerLockPath)
	if err != nil {
		return false, fmt.Errorf("read consumer lock %s: %w", consumerLockPath, err)
	}
	var lock consumerLock
	if err := json.Unmarshal(raw, &lock); err != nil {
		return false, fmt.Errorf("unmarshal consumer lock: %w", err)
	}
	if len(lock.Contracts) == 0 {
		return false, fmt.Errorf("no contracts recorded in %s", consumerLockPath)
	}
	c := lock.Contracts[0]

	specSHA, err := fileSHA256(candidateSpec)
	if err != nil {
		return false, fmt.Errorf("sha256 candidate spec %s: %w", candidateSpec, err)
	}
	matches := (specSHA == c.SHA256)
	fmt.Printf("[lock-check] candidate spec: %s (lock: %s, match: %v)\n", specSHA, c.SHA256, matches)

	for _, ex := range c.Examples {
		baseName := filepath.Base(ex.Path)
		candidateExPath := filepath.Join(candidateExamplesDir, baseName)
		exSHA, err := fileSHA256(candidateExPath)
		if err != nil {
			fmt.Printf("[lock-check] candidate example %s missing or unreadable: %v\n", baseName, err)
			matches = false
			continue
		}
		if exSHA != ex.SHA256 {
			fmt.Printf("[lock-check] example %s sha256 mismatch: got %s, lock %s\n", baseName, exSHA, ex.SHA256)
			matches = false
		}
	}
	return matches, nil
}

type operationCheck struct {
	name       string
	path       string
	call       func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error)
	rawQuery   string
	rowDecimal []string
	rowInteger []string
	rowKeys    []string
	// nested validates array-valued keys inside each row against a closed schema.
	nested []nestedArrayCheck
	// minRows is the fewest rows the fixture must return for this operation.
	minRows int
}

// wireKind is the declared JSON type of one nested field (provider contract).
type wireKind int

const (
	kindStringOrNull wireKind = iota
	kindCodeOrNull
	kindProseOrNull
	kindDecimalOrNull
	kindBool
	kindDateTimeOrNull
	kindPnlClass
)

type nestedField struct {
	name string
	kind wireKind
}

// nestedArrayCheck is a closed schema for the elements of one array-valued key:
// every listed key is required, and no key outside the list is allowed.
type nestedArrayCheck struct {
	field  string
	fields []nestedField
}

// wireCounts records how many rows and nested elements one response checked.
type wireCounts struct {
	rows   int
	nested int
}

var decimalPattern = regexp.MustCompile(`^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$`)

var codePattern = regexp.MustCompile(`^[A-Za-z0-9_.:\-]+$`)

var pnlClassValues = map[string]bool{
	"recorded": true, "not_applicable": true, "unknown": true, "missing": true,
}

// retroTradesRowKeys is the closed TradeRow key set from contracts/retro/v1/openapi.yaml.
var retroTradesRowKeys = []string{
	"trade_ref", "symbol", "market", "side", "strategy", "entry_at", "exit_at",
	"quantity", "entry_price", "exit_price", "currency", "return_rate",
	"realized_amount", "realized_currency", "exit_reason", "basis", "reviews",
}

// retroTradesReviews is the closed TradeReviewRow schema (additionalProperties: false,
// all fourteen keys required) with the declared type of each field.
var retroTradesReviews = nestedArrayCheck{
	field: "reviews",
	fields: []nestedField{
		{"review_ref", kindCodeOrNull},
		{"outcome", kindCodeOrNull},
		{"trigger_type", kindCodeOrNull},
		{"root_cause_class", kindCodeOrNull},
		{"pnl_status", kindCodeOrNull},
		{"pnl_class", kindPnlClass},
		{"realized_pnl", kindDecimalOrNull},
		{"realized_pnl_currency", kindCodeOrNull},
		{"fill_evidence_available", kindBool},
		{"created_at", kindDateTimeOrNull},
		{"lesson", kindProseOrNull},
		{"result_summary", kindProseOrNull},
		{"rationale", kindProseOrNull},
		{"next_strategy", kindProseOrNull},
	},
}

// retroTradesOperation is the retro_trades check definition, shared by the gate
// run and the unit tests so the nested review schema cannot drift between them.
// The call function is set by the caller; the unit tests only validate wire bytes.
func retroTradesOperation() operationCheck {
	return operationCheck{
		name:       "retro_trades",
		path:       "/trading/api/retro/v1/retro_trades",
		rawQuery:   "?from=2026-01-01&to=2026-01-10",
		rowKeys:    retroTradesRowKeys,
		rowDecimal: []string{"quantity", "entry_price", "exit_price", "return_rate", "realized_amount"},
		nested:     []nestedArrayCheck{retroTradesReviews},
		minRows:    1,
	}
}

func checkKind(v any, kind wireKind) error {
	switch kind {
	case kindStringOrNull:
		if v == nil {
			return nil
		}
		if _, ok := v.(string); !ok {
			return fmt.Errorf("must be a string or null, got %T (%v)", v, v)
		}
	case kindCodeOrNull:
		if v == nil {
			return nil
		}
		s, ok := v.(string)
		if !ok {
			return fmt.Errorf("must be a string or null, got %T (%v)", v, v)
		}
		if !codePattern.MatchString(s) {
			return fmt.Errorf("code %q does not match the contract pattern ^[A-Za-z0-9_.:\\-]+$", s)
		}
		if n := utf8.RuneCountInString(s); n > 64 {
			return fmt.Errorf("code is %d characters, the contract allows at most 64", n)
		}
	case kindProseOrNull:
		if v == nil {
			return nil
		}
		s, ok := v.(string)
		if !ok {
			return fmt.Errorf("must be a string or null, got %T (%v)", v, v)
		}
		if n := utf8.RuneCountInString(s); n > 1000 {
			return fmt.Errorf("prose is %d characters, the contract allows at most 1000", n)
		}
	case kindDecimalOrNull:
		if v == nil {
			return nil
		}
		s, ok := v.(string)
		if !ok {
			return fmt.Errorf("decimal must be a string, got %T (%v)", v, v)
		}
		if !decimalPattern.MatchString(s) {
			return fmt.Errorf("decimal %q does not match the exact-decimal pattern", s)
		}
	case kindBool:
		if _, ok := v.(bool); !ok {
			return fmt.Errorf("must be a boolean, got %T (%v)", v, v)
		}
	case kindDateTimeOrNull:
		if v == nil {
			return nil
		}
		s, ok := v.(string)
		if !ok {
			return fmt.Errorf("date-time must be a string, got %T (%v)", v, v)
		}
		if _, err := time.Parse(time.RFC3339Nano, s); err != nil {
			return fmt.Errorf("date-time %q is not RFC 3339: %w", s, err)
		}
	case kindPnlClass:
		s, ok := v.(string)
		if !ok || !pnlClassValues[s] {
			return fmt.Errorf("must be one of recorded|not_applicable|unknown|missing, got %v", v)
		}
	}
	return nil
}

// validateNestedRow applies one closed nested schema to one element of an array.
func validateNestedRow(item any, na nestedArrayCheck, op string, row, idx int) error {
	obj, ok := item.(map[string]any)
	if !ok {
		return fmt.Errorf("schema violation: operation %s row %d %s %d is not an object", op, row, na.field, idx)
	}
	allowed := make(map[string]bool, len(na.fields))
	for _, f := range na.fields {
		allowed[f.name] = true
	}
	for _, f := range na.fields {
		v, ok := obj[f.name]
		if !ok {
			return fmt.Errorf("schema violation: operation %s row %d %s %d missing required field %q", op, row, na.field, idx, f.name)
		}
		if err := checkKind(v, f.kind); err != nil {
			return fmt.Errorf("schema violation: operation %s row %d %s %d field %q: %w", op, row, na.field, idx, f.name, err)
		}
	}
	keys := make([]string, 0, len(obj))
	for k := range obj {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if !allowed[k] {
			return fmt.Errorf("schema violation: operation %s row %d %s %d unexpected field %q (not in the closed schema)", op, row, na.field, idx, k)
		}
	}
	return nil
}

// checkCoverage fails when the fixture did not give the harness anything to check.
func checkCoverage(op operationCheck, counts wireCounts) error {
	if counts.rows < op.minRows {
		return fmt.Errorf("coverage violation: operation %s returned %d rows, want at least %d", op.name, counts.rows, op.minRows)
	}
	if len(op.nested) > 0 && counts.nested == 0 {
		return fmt.Errorf("coverage violation: operation %s checked 0 nested elements; the gate fixture must include a linked review", op.name)
	}
	return nil
}

func scanPrivacy(val any, path string) error {
	switch v := val.(type) {
	case string:
		if accountNoPattern.MatchString(v) {
			return fmt.Errorf("privacy violation at %s: contains account number pattern (%q)", path, v)
		}
		if brokerOrdPattern.MatchString(v) {
			return fmt.Errorf("privacy violation at %s: contains broker order ID pattern (%q)", path, v)
		}
		if tracebackPattern.MatchString(v) {
			return fmt.Errorf("privacy violation at %s: contains internal exception / traceback (%q)", path, v)
		}
	case map[string]any:
		for k, child := range v {
			if err := scanPrivacy(child, path+"."+k); err != nil {
				return err
			}
		}
	case []any:
		for i, child := range v {
			if err := scanPrivacy(child, fmt.Sprintf("%s[%d]", path, i)); err != nil {
				return err
			}
		}
	}
	return nil
}

func validateWireEnvelope(raw map[string]any, op operationCheck) (wireCounts, error) {
	var counts wireCounts
	envelopeRequired := []string{
		"as_of", "data_as_of", "timezone", "total", "truncated",
		"limit", "cursor", "next_cursor", "unknowns", "data",
	}
	for _, key := range envelopeRequired {
		if _, ok := raw[key]; !ok {
			return wireCounts{}, fmt.Errorf("schema violation: operation %s: missing envelope field %q", op.name, key)
		}
	}

	// Validate unknown / gap presentation
	totalVal := raw["total"]
	if totalVal == nil {
		unknowns, ok := raw["unknowns"].([]any)
		if !ok {
			return wireCounts{}, fmt.Errorf("unknowns violation: operation %s: total is null but unknowns is not a list", op.name)
		}
		explained := false
		for _, u := range unknowns {
			if um, ok := u.(map[string]any); ok {
				if um["field"] == "total" && um["reason"] == "count_unavailable" {
					explained = true
					break
				}
			}
		}
		if !explained {
			return wireCounts{}, fmt.Errorf("unknowns violation: operation %s: total is null but unknowns does not explain field=total with reason=count_unavailable", op.name)
		}
	} else if num, ok := totalVal.(json.Number); ok {
		if strings.Contains(num.String(), ".") {
			return wireCounts{}, fmt.Errorf("precision violation: operation %s: total is serialized as float %s", op.name, num.String())
		}
	}

	// Validate pagination next_cursor
	if cursorVal := raw["next_cursor"]; cursorVal != nil {
		cursorStr, ok := cursorVal.(string)
		if !ok {
			return wireCounts{}, fmt.Errorf("pagination violation: operation %s: next_cursor is not a string", op.name)
		}
		if op.name == "retro_scoreboard" {
			if !scoreCursorPat.MatchString(cursorStr) {
				return wireCounts{}, fmt.Errorf("pagination violation: operation %s: invalid scoreboard cursor %q", op.name, cursorStr)
			}
		} else {
			if !cursorPattern.MatchString(cursorStr) {
				return wireCounts{}, fmt.Errorf("pagination violation: operation %s: invalid next_cursor %q", op.name, cursorStr)
			}
		}
	}

	// Validate data array and rows
	dataArr, ok := raw["data"].([]any)
	if !ok {
		return wireCounts{}, fmt.Errorf("schema violation: operation %s: data is not a list", op.name)
	}

	for i, rowItem := range dataArr {
		row, ok := rowItem.(map[string]any)
		if !ok {
			return wireCounts{}, fmt.Errorf("schema violation: operation %s row %d is not an object", op.name, i)
		}
		for _, reqKey := range op.rowKeys {
			if _, ok := row[reqKey]; !ok {
				return wireCounts{}, fmt.Errorf("schema violation: operation %s row %d missing required field %q", op.name, i, reqKey)
			}
		}
		for _, decKey := range op.rowDecimal {
			if v, ok := row[decKey]; ok && v != nil {
				if _, isStr := v.(string); !isStr {
					return wireCounts{}, fmt.Errorf("precision violation: operation %s row %d decimal field %q must be string, got %T (%v)", op.name, i, decKey, v, v)
				}
			}
		}
		for _, intKey := range op.rowInteger {
			if v, ok := row[intKey]; ok && v != nil {
				num, isNum := v.(json.Number)
				if !isNum {
					return wireCounts{}, fmt.Errorf("precision violation: operation %s row %d integer field %q must be number, got %T (%v)", op.name, i, intKey, v, v)
				}
				if strings.Contains(num.String(), ".") {
					return wireCounts{}, fmt.Errorf("precision violation: operation %s row %d integer field %q serialized as float: %s", op.name, i, intKey, num.String())
				}
			}
		}
		for _, na := range op.nested {
			items, ok := row[na.field].([]any)
			if !ok {
				return wireCounts{}, fmt.Errorf("schema violation: operation %s row %d field %q must be a list", op.name, i, na.field)
			}
			for j, item := range items {
				if err := validateNestedRow(item, na, op.name, i, j); err != nil {
					return wireCounts{}, err
				}
				counts.nested++
			}
		}
		counts.rows++
	}

	// Privacy scan
	if err := scanPrivacy(raw, op.name); err != nil {
		return wireCounts{}, err
	}
	return counts, nil
}

func runChecks(baseURL, token string) error {
	tokenFilePath := filepath.Join(os.TempDir(), fmt.Sprintf("retro-token-%d.txt", os.Getpid()))
	if err := os.WriteFile(tokenFilePath, []byte(token+"\n"), 0o600); err != nil {
		return fmt.Errorf("write token file: %w", err)
	}
	defer os.Remove(tokenFilePath)

	provider, err := autotrader.New(autotrader.Config{
		BaseURL:   baseURL,
		TokenFile: tokenFilePath,
		Timeout:   10 * time.Second,
	})
	if err != nil {
		return fmt.Errorf("autotrader.New: %w", err)
	}

	d := parseDate
	retroTrades := retroTradesOperation()
	retroTrades.call = func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
		return p.RetroTrades(ctx, &autotrader.RetroTradesParams{From: d("2026-01-01"), To: d("2026-01-10")})
	}
	operations := []operationCheck{
		{
			name: "retro_day",
			path: "/trading/api/retro/v1/retro_day",
			rawQuery: "?market=kr&date=2026-01-05",
			call: func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
				return p.RetroDay(ctx, &autotrader.RetroDayParams{Market: "kr", Date: d("2026-01-05")})
			},
			rowKeys:    []string{"symbol", "stage", "event_at", "event_ref", "linked_ref", "status", "reason", "quantity", "price", "currency", "unit", "basis"},
			rowDecimal: []string{"quantity", "price"},
		},
		{
			name: "retro_symbol",
			path: "/trading/api/retro/v1/retro_symbol",
			rawQuery: "?symbol=005930&from=2026-01-01&to=2026-01-10",
			call: func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
				return p.RetroSymbol(ctx, &autotrader.RetroSymbolParams{Symbol: "005930", From: d("2026-01-01"), To: d("2026-01-10")})
			},
			rowKeys:    []string{"symbol", "stage", "event_at", "event_ref", "linked_ref", "status", "reason", "quantity", "price", "currency", "unit", "basis"},
			rowDecimal: []string{"quantity", "price"},
		},
		{
			name: "retro_funnel",
			path: "/trading/api/retro/v1/retro_funnel",
			rawQuery: "?market=kr&from=2026-01-01&to=2026-01-10",
			call: func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
				return p.RetroFunnel(ctx, &autotrader.RetroFunnelParams{Market: "kr", From: d("2026-01-01"), To: d("2026-01-10")})
			},
			rowKeys:    []string{"stage", "cohort", "count", "denominator", "fill_rate", "unit", "basis", "missing_reason"},
			rowDecimal: []string{"fill_rate"},
			rowInteger: []string{"count", "denominator"},
		},
		{
			name: "retro_scoreboard",
			path: "/trading/api/retro/v1/retro_scoreboard",
			rawQuery: "?from=2026-01-01&to=2026-01-08",
			call: func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
				return p.RetroScoreboard(ctx, &autotrader.RetroScoreboardParams{From: d("2026-01-01"), To: d("2026-01-08")})
			},
			rowKeys:    []string{"scope", "subject_ref", "market", "basis", "evidence_basis", "return_rate", "unmeasured_reason", "flows_complete", "start_amount", "end_amount", "flow_amount", "currency", "unit", "benchmark"},
			rowDecimal: []string{"return_rate", "start_amount", "end_amount", "flow_amount"},
		},
		{
			name: "retro_positions",
			path: "/trading/api/retro/v1/retro_positions",
			rawQuery: "?cutoff_date=2026-01-05",
			call: func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
				return p.RetroPositions(ctx, &autotrader.RetroPositionsParams{CutoffDate: d("2026-01-05")})
			},
			rowKeys:    []string{"symbol", "position_ref", "quantity", "average_price", "unrealized_amount", "currency", "unit", "basis", "lot_state", "missing_reason"},
			rowDecimal: []string{"quantity", "average_price", "unrealized_amount"},
		},
		{
			name: "mock_experiments",
			path: "/trading/api/retro/v1/mock_experiments",
			rawQuery: "?strategy_id=S_ALPHA",
			call: func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
				strat := "S_ALPHA"
				return p.MockExperiments(ctx, &autotrader.MockExperimentsParams{StrategyId: &strat})
			},
			rowKeys:    []string{"strategy_id", "experiment_ref", "environment", "planned_count", "sent_count", "filled_count", "uncertain_count", "episode_pnl_amount", "currency", "unit", "basis"},
			rowDecimal: []string{"episode_pnl_amount"},
			rowInteger: []string{"planned_count", "sent_count", "filled_count", "uncertain_count"},
		},
		{
			name: "get_bars",
			path: "/trading/api/retro/v1/get_bars",
			rawQuery: "?symbol=005930&tf=1d&from=2026-01-01&to=2026-01-10",
			call: func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
				return p.GetBars(ctx, &autotrader.GetBarsParams{Symbol: "005930", Tf: autotrader.N1d, From: d("2026-01-01"), To: d("2026-01-10")})
			},
			rowKeys:    []string{"symbol", "time", "open_price", "high_price", "low_price", "close_price", "quantity", "currency", "unit", "source", "venue", "adjusted", "missing_reason", "basis"},
			rowDecimal: []string{"open_price", "high_price", "low_price", "close_price", "quantity"},
		},
		retroTrades,
		{
			name: "data_gaps",
			path: "/trading/api/retro/v1/data_gaps",
			rawQuery: "",
			call: func(ctx context.Context, p *autotrader.Provider) (autotrader.Result, error) {
				return p.DataGaps(ctx, &autotrader.DataGapsParams{})
			},
			rowKeys: []string{"dataset", "symbol", "last_updated_at", "source", "venue", "adjusted", "missing_reason", "basis"},
		},
	}

	ctx := context.Background()
	httpClient := &http.Client{Timeout: 10 * time.Second}

	for _, op := range operations {
		// 1. Call via consumer client wrapper
		res, err := op.call(ctx, provider)
		if err != nil {
			return fmt.Errorf("consumer client call %s failed: %w", op.name, err)
		}
		if res == nil {
			return fmt.Errorf("consumer client call %s returned nil result", op.name)
		}

		// 2. Query raw JSON to check wire-level format, precision and exact fields
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+op.path+op.rawQuery, nil)
		if err != nil {
			return fmt.Errorf("build request for %s: %w", op.name, err)
		}
		req.Header.Set(autotrader.TokenHeader, token)
		resp, err := httpClient.Do(req)
		if err != nil {
			return fmt.Errorf("raw request for %s: %w", op.name, err)
		}
		bodyBytes, err := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if err != nil {
			return fmt.Errorf("read body for %s: %w", op.name, err)
		}
		if resp.StatusCode != http.StatusOK {
			return fmt.Errorf("operation %s returned status %d, want 200 (body: %s)", op.name, resp.StatusCode, string(bodyBytes))
		}

		dec := json.NewDecoder(bytes.NewReader(bodyBytes))
		dec.UseNumber()
		var raw map[string]any
		if err := dec.Decode(&raw); err != nil {
			return fmt.Errorf("decode json for %s: %w", op.name, err)
		}

		counts, err := validateWireEnvelope(raw, op)
		if err != nil {
			return err
		}
		if err := checkCoverage(op, counts); err != nil {
			return err
		}
		fmt.Printf("[operation-verified] %s: OK (rows=%d nested=%d)\n", op.name, counts.rows, counts.nested)
	}

	// 3. Authorization boundary checks
	// (a) Unauthenticated request -> must be 401
	unauthReq, _ := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/trading/api/retro/v1/data_gaps", nil)
	unauthResp, err := httpClient.Do(unauthReq)
	if err != nil {
		return fmt.Errorf("unauth request failed: %w", err)
	}
	_ = unauthResp.Body.Close()
	if unauthResp.StatusCode != http.StatusUnauthorized {
		return fmt.Errorf("auth violation: unauthenticated request returned status %d, want 401", unauthResp.StatusCode)
	}
	fmt.Println("[auth-check] unauthenticated request -> 401: OK")

	// (b) Wrong token request -> must be 401
	wrongReq, _ := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/trading/api/retro/v1/data_gaps", nil)
	wrongReq.Header.Set(autotrader.TokenHeader, "wrong-retro-token")
	wrongResp, err := httpClient.Do(wrongReq)
	if err != nil {
		return fmt.Errorf("wrong token request failed: %w", err)
	}
	_ = wrongResp.Body.Close()
	if wrongResp.StatusCode != http.StatusUnauthorized {
		return fmt.Errorf("auth violation: request with wrong token returned status %d, want 401", wrongResp.StatusCode)
	}
	fmt.Println("[auth-check] wrong token request -> 401: OK")

	// (c) Disallowed method (POST) -> must be 403 or 405
	postReq, _ := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/trading/api/retro/v1/data_gaps", nil)
	postReq.Header.Set(autotrader.TokenHeader, token)
	postResp, err := httpClient.Do(postReq)
	if err != nil {
		return fmt.Errorf("post request failed: %w", err)
	}
	_ = postResp.Body.Close()
	if postResp.StatusCode != http.StatusForbidden && postResp.StatusCode != http.StatusMethodNotAllowed {
		return fmt.Errorf("auth violation: POST request returned status %d, want 403 or 405", postResp.StatusCode)
	}
	fmt.Println("[auth-check] disallowed POST method -> 403/405: OK")

	return nil
}

func main() {
	baseURL := flag.String("base-url", "", "candidate provider base URL (e.g. http://127.0.0.1:8825)")
	token := flag.String("token", "", "X-Retro-Read-Token value")
	candidateSpec := flag.String("candidate-spec", "", "path to candidate openapi.yaml")
	candidateExamples := flag.String("candidate-examples", "", "path to candidate examples dir")
	consumerLockPath := flag.String("consumer-lock", "", "path to consumer contracts.lock.json")
	flag.Parse()

	if *baseURL == "" {
		fmt.Fprintln(os.Stderr, "error: --base-url is required")
		os.Exit(2)
	}
	if *token == "" {
		fmt.Fprintln(os.Stderr, "error: --token is required")
		os.Exit(2)
	}

	if *consumerLockPath != "" && *candidateSpec != "" && *candidateExamples != "" {
		matches, err := verifyContractLock(*consumerLockPath, *candidateSpec, *candidateExamples)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error verifying contract lock: %v\n", err)
			os.Exit(1)
		}
		if matches {
			fmt.Println("[lock-check] candidate contract matches consumer lock byte-for-byte")
		} else {
			fmt.Println("[lock-check] candidate contract differs from consumer lock; exercising client for compatibility")
		}
	}

	if err := runChecks(*baseURL, *token); err != nil {
		fmt.Fprintf(os.Stderr, "GATE FAILURE: %v\n", err)
		os.Exit(1)
	}

	fmt.Println("\n=======================================================")
	fmt.Println("RETRO CONSUMER GATE: ALL CONTRACT ASSERTIONS PASSED (GREEN)")
	fmt.Println("=======================================================")
}

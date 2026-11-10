// Package main implements the Go contract gate harness for auto_trader retro/v1.
// It uses the pinned factortrail Go consumer client to invoke all eight retro/v1
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
	"strings"
	"time"

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

func validateWireEnvelope(raw map[string]any, op operationCheck) error {
	envelopeRequired := []string{
		"as_of", "data_as_of", "timezone", "total", "truncated",
		"limit", "cursor", "next_cursor", "unknowns", "data",
	}
	for _, key := range envelopeRequired {
		if _, ok := raw[key]; !ok {
			return fmt.Errorf("schema violation: operation %s: missing envelope field %q", op.name, key)
		}
	}

	// Validate unknown / gap presentation
	totalVal := raw["total"]
	if totalVal == nil {
		unknowns, ok := raw["unknowns"].([]any)
		if !ok {
			return fmt.Errorf("unknowns violation: operation %s: total is null but unknowns is not a list", op.name)
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
			return fmt.Errorf("unknowns violation: operation %s: total is null but unknowns does not explain field=total with reason=count_unavailable", op.name)
		}
	} else if num, ok := totalVal.(json.Number); ok {
		if strings.Contains(num.String(), ".") {
			return fmt.Errorf("precision violation: operation %s: total is serialized as float %s", op.name, num.String())
		}
	}

	// Validate pagination next_cursor
	if cursorVal := raw["next_cursor"]; cursorVal != nil {
		cursorStr, ok := cursorVal.(string)
		if !ok {
			return fmt.Errorf("pagination violation: operation %s: next_cursor is not a string", op.name)
		}
		if op.name == "retro_scoreboard" {
			if !scoreCursorPat.MatchString(cursorStr) {
				return fmt.Errorf("pagination violation: operation %s: invalid scoreboard cursor %q", op.name, cursorStr)
			}
		} else {
			if !cursorPattern.MatchString(cursorStr) {
				return fmt.Errorf("pagination violation: operation %s: invalid next_cursor %q", op.name, cursorStr)
			}
		}
	}

	// Validate data array and rows
	dataArr, ok := raw["data"].([]any)
	if !ok {
		return fmt.Errorf("schema violation: operation %s: data is not a list", op.name)
	}

	for i, rowItem := range dataArr {
		row, ok := rowItem.(map[string]any)
		if !ok {
			return fmt.Errorf("schema violation: operation %s row %d is not an object", op.name, i)
		}
		for _, reqKey := range op.rowKeys {
			if _, ok := row[reqKey]; !ok {
				return fmt.Errorf("schema violation: operation %s row %d missing required field %q", op.name, i, reqKey)
			}
		}
		for _, decKey := range op.rowDecimal {
			if v, ok := row[decKey]; ok && v != nil {
				if _, isStr := v.(string); !isStr {
					return fmt.Errorf("precision violation: operation %s row %d decimal field %q must be string, got %T (%v)", op.name, i, decKey, v, v)
				}
			}
		}
		for _, intKey := range op.rowInteger {
			if v, ok := row[intKey]; ok && v != nil {
				num, isNum := v.(json.Number)
				if !isNum {
					return fmt.Errorf("precision violation: operation %s row %d integer field %q must be number, got %T (%v)", op.name, i, intKey, v, v)
				}
				if strings.Contains(num.String(), ".") {
					return fmt.Errorf("precision violation: operation %s row %d integer field %q serialized as float: %s", op.name, i, intKey, num.String())
				}
			}
		}
	}

	// Privacy scan
	if err := scanPrivacy(raw, op.name); err != nil {
		return err
	}
	return nil
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

		if err := validateWireEnvelope(raw, op); err != nil {
			return err
		}
		fmt.Printf("[operation-verified] %s: OK\n", op.name)
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

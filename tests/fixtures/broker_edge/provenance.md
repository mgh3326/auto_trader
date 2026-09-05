# Witness fixture provenance
Capture method: in-process `httptest.NewRecorder` at the exact commit below, 2026-09-05 KST.
Source repository: https://github.com/mgh3326/broker-edge
Exact source commit: ec20a949c0a4d15aa62f8501067e0237fd41c212 (merged PR #23).
Source checkout: exact commit above, detached and clean after capture.

The source repository has no fixed receipt/audit JSON fixture files. These files are the exact bytes emitted by the existing internal/kismockedge.NewHandler using httptest.NewRecorder, the original testLiveWitnessCommand helper, and the fixed clock from TestKISLiveWitnessEchoAndMissingEchoAudit. No JSON output was reformatted. echo-request.json is the verbatim byte literal in witness_test.go.

Reproduction probe: witness-fixture-capture.go.txt (copy to internal/kismockedge/captain_fixture_capture_test.go in the exact source worktree, run the command below, then remove the temporary probe).
Command: go test -count=1 -v ./internal/kismockedge -run '^TestCaptainCaptureWitnessFixtures$'
Result: PASS, exit 0; raw stdout/stderr in witness-fixture-capture-output.txt. Byte hashes in witness-fixture-sha256.json.

The test used only a new in-memory SQLite test store initialized by the repository's existing test helper. No real database, deployment, migration command, network listener, HTTP client, broker, environment file, or existing service was touched. Product source was not changed.

Important actual response shape: an empty missing-echo audit is {"witnesses":null}, not an empty array. Both null and populated arrays must be interpreted as the service actually returns them; missing the witnesses property or a malformed value remains an error.

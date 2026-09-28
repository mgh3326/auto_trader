# Task 916: stopped KIS WebSocket during an explicit skip

Frozen before implementation. Purpose: allow a real deploy or manual rollback to preserve an intentionally stopped at-kis-ws when the operator supplies --skip-kis-ws, without weakening any other capture or digest failure.

| Invariant | Input and transition | Enforcement point | Dependencies | Independent observation |
| --- | --- | --- | --- | --- |
| Only the exact skipped, stopped KIS unit gets the exemption. | --skip-kis-ws is set; at-kis-ws exists and reports State.Running=false; capture records its last resolvable immutable digest, or UNKNOWN if unavailable. | capture_initial_state, after confirmed presence and running-state inspection, before rejecting a stopped unit. | Docker inspect, unit_rollback_image; no container mutation. | Real deploy reaches docker pull and promotion; KIS has no run, stop, rm, or rename call. |
| Digest verification names the exemption and does not fail on that row. | The same skip and stopped state yields SKIPPED_STOPPED, with the captured digest in expected and STOPPED in running. | report_digests, after running_digest and before mismatch comparison. | EXPECTED_IMAGES from capture. | Digest table row shows SKIPPED_STOPPED and last known digest when available; deploy exits zero if all other rows match. |
| Every other stopped unit remains a failure. | Any unit other than at-kis-ws is stopped, or at-kis-ws is stopped without --skip-kis-ws. | capture_initial_state. | Exact unit name and explicit flag. | Capture exits before pull or mutation. |
| A running skipped KIS unit retains prior behavior. | --skip-kis-ws is set; at-kis-ws reports State.Running=true. | Existing capture and report_digests paths. | Immutable digest resolution still required. | Its digest is printed as MATCH and the container is untouched. |
| Real mismatches still roll back. | A non-exempt row differs after promotion. | report_digests return value and promote_digest failure branch. | Replacement log and original images. | Nonzero result, MISMATCH row, and full restoration of replaced units. |

Counterexamples that must fail the acceptance suite: a skip flag exempts a stopped worker; no skip exempts a stopped KIS unit; a running KIS unit is silently treated as stopped; a digest mismatch in another unit exits zero or avoids rollback; a test claims coverage using dry-run, which never calls capture_initial_state; a stopped skipped KIS unit is removed or restarted; an unresolved digest is invented as an immutable digest.

Assertion-RED mutants, each expressed as an invariant violation: remove the stopped-KIS capture exemption; make the exemption apply without the flag; make it apply to every stopped unit; remove the digest-table SKIPPED_STOPPED classification; make report_digests ignore a real non-KIS mismatch; alter the running-KIS skip path to show SKIPPED_STOPPED. Each mutant must fail by an assertion in the focused tests, then the unmodified script must pass.

Acceptance commands run only against the fake Docker and curl shim: bash -n scripts/deploy-ncp-pull.sh; uv run pytest tests/scripts/test_deploy_ncp_pull_rollback.py tests/scripts/test_deploy_ncp_pull_digest_pin.py -q; focused assertion-RED mutant runs with DEPLOY_UNDER_TEST pointing at a temporary local script copy. No production host, app, MCP server, broker, database, or real env file participates.

# fillwire E1 contract fixtures

These files are unedited copies from fillwire. Do not change them by hand. When fillwire re-records them, copy them again and update this file.

- Repository: `mgh3326/fillwire`, PR #20 (ROB-1457)
- Source commit: `9bf7604c0110a2591b36d6d97cd2b3eae8e7f55e`
- Source path: `internal/exec/e1/testdata/`
- Recorded from auto_trader commit: `b4ddb1f2e8edab08498802085715e837bad640fb`

| File | SHA-256 |
|---|---|
| `e1_scenarios.json` | `27338e51256b2a562144f6a677569a330886617c24178ad8aad9c6ed0c6500c1` |
| `e1_recorded.json` | `4d6ace5d4d7ba4a1cd4746c81fa82f96a74482a8d2beaa49eb9b7bfaf6682379` |

`tests/services/execution_intent_gateway/test_http_contract_fixtures.py` checks the hashes and replays every scenario through the HTTP route.

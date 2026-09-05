# KIS live shadow witness fixtures

These files are byte-for-byte copies of the broker-edge `ec20a949c0a4d15aa62f8501067e0237fd41c212`
capture supplied by an independent fixture-preparation run. `kis_live_shadow_witness_v1.schema.json` is the
upstream execution-contract schema, copied without comments or modification.

To update: capture fresh bytes with broker-edge's in-process `httptest.NewRecorder`
probe, record its source commit and SHA-256 manifest, then replace this schema and
the runtime schema together. Do not fetch upstream in CI; contract tests validate
the local fixture against the current wire payload.

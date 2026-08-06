# CR-S1 B2 — artifact-only top-level resynthesis

CR-S1 keeps its completed full and ablation arms immutable. B2 reads only the
six pair JSON artifacts and emits a separate result; it neither imports the
daily corpus nor calls the Stage-B execution engine. This preserves the
one-time run budget and makes the result reversible by discarding the new
output artifact.

## Derived INCONCLUSIVE predicate

The frozen falsification contract requires `FRAGILE` as a mandatory output:
remove the highest-performing contributing year and determine whether the sign
reverses. That calculation requires two non-empty annual full-versus-ablation
comparisons: one to remove and one remaining comparison. Consequently B2 does
not assert a top-level verdict when the calculation is impossible:

- no full or ablation arm has an annual net-return output:
  `INCONCLUSIVE_EMPTY_ALL_ARMS`;
- otherwise, fewer than two years have finite full, ablation, and incremental
  net means: `INCONCLUSIVE_INSUFFICIENT_JUDGEABLE_YEARS`.

This is a direct consequence of the mandatory FRAGILE calculation, **not a
new minimum-sample-size gate**. The predicate has priority after an existing
`RUN_INVALID*` label and before an existing `FALSIFIED*` or
`(NOT_)FALSIFIED`/`PASS` label. When it does not apply, B2 preserves the exact
existing label.

Every candidate × venue result's `headline` contains `judgeable_years` and
`fragile`. They are disclosures: `fragile` never controls verdict selection,
and `judgeable_years` is consulted only by the derived inability-to-calculate-
FRAGILE predicate above.

## Offline invocation

```bash
uv run python -m scripts.resynthesize_cr_s1_b2 \
  --source-dir /path/to/cr-s1-run-r2-output \
  --output-dir /new/path/cr-s1-b2-resynthesis
```

The command checks all relay-pinned SHA-256 values before parsing any source
JSON. It writes canonical JSON with atomic no-overwrite publication and rejects
an output directory equal to or below the immutable source directory.

# B0X portability operator-contract projection

This public artifact records only content digests for the two approved,
read-only policy sources and value-free results for each reviewed invariant
class. Exact operator field names, assignments, thresholds, exception names,
and authority wording remain in the private task evidence; they are not runtime
configuration and are intentionally not copied here.

The machine-readable attestation is
`config/b0x_operator_contract_projection.json`. Its exact source-digest keys,
field-class set, per-class `matched` result, empty conflict list, and authority
guards all fail closed in CI. Existing production envelope constants are also
bound by a canonical digest without duplicating their values in this artifact.

| Reviewed invariant class | Public result |
| --- | --- |
| source slot, market, weekend, and playbook semantics | matched; no conflicts |
| strategy assignment and sole-writer authority | matched; no conflicts |
| account and market mapping | matched; no conflicts |
| execution envelopes and thresholds | matched; no conflicts |
| temporary coexistence constraint | matched; no conflicts |
| strategy order exceptions | matched; no conflicts |
| scoring and promotion prohibitions | matched; no conflicts |
| approved order path | matched; no conflicts |

Task 191 changes transport, hermetic replay, and installer-supplied paths only.
It does not alter any of the reviewed semantics or grant mutation authority to
the observation-only shadow profile.

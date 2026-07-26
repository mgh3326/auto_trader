# ROB-1040 CRS-24 closeout runbook

Status: **TERMINATE_RESEARCH** as of 2026-07-26. The CRS-24 one-shot is
consumed. This document records the launcher boundary for audit and
maintenance; it does not authorize another empirical run.

## Threat model and provenance boundary

The launcher guards against operator error and accidental source or artifact
drift. Arbitrary code execution inside the same Python process is outside its
threat model: such code could replace the evaluator itself, so adding more
in-process object guards would not provide a meaningful security boundary.

The real-posture must-differ pins are a **negative test** only: they establish
that a `CampaignInputBinding` is not the frozen synthetic fixture. They do not
affirmatively establish that the binding came from the frozen ROB-941 corpus.

Affirmative corpus provenance is provided entirely by the launcher's
manifest-lineage chain:

- `rob974_lineage.verify_parent` checks the pinned physical manifest digest
  and canonical manifest-content digest.
- `EXPECTED_PARENT_CONTENT_SHA256` independently binds the launcher to that
  canonical parent identity.
- `rob941_offline_loader` verifies every shard's raw-archive checksum chain,
  derived shard bytes, schema, canonical row content, and declared bounds.

The negative pins and affirmative lineage chain are complementary. A
must-differ PASS alone must never be reported as proof of ROB-941 provenance.

## Git gate behavior

The exact-main gate performs `git fetch --prune origin` itself and refuses if
the fetch fails, so it cannot pass from only a stale or locally forged
`origin/main` ref. The refreeze-ancestor gate detects a shallow checkout and
unshallows it before making the final ancestry decision.

## Preserved one-shot evidence

The completed evidence remains under
`herdr-artifacts/rob1040-oneshot-56bdb1f6/`. Its canonical digest is
`56bdb1f6aa425ed42c4137fb494946171d3634d0fb4a8dbb4a3231ecb79c2731`.
The judgment report's reproduction procedure reads the launcher and sealed
files from commit `c9b4658b`; closeout maintenance must not rewrite that
commit or reinterpret the terminated result.

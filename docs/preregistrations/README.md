# Pre-registrations

Frozen, dated experiment declarations. A file in this directory records what an
experiment claimed **before** any of its outcomes were seen.

## The one rule

🔴 **A registered pre-registration is never edited in place.** Not to fix
scope, not to widen a window, not to soften a promotion threshold, and
especially not after seeing an intermediate result. To change one, add a new
dated file that names the one it supersedes, and leave the original standing.

That immutability is the entire value. A pre-registration you can edit is a
post-registration, and a post-registration proves nothing.

Typo fixes and added cross-references are the only permitted in-place edits,
and they must not touch any of: hypothesis, variant definitions, lanes/markets,
collection window, sample target, recorded fields, promotion conditions.

## What belongs here vs elsewhere

| Artifact | Home | Mutable? |
| --- | --- | --- |
| The frozen claim | `docs/preregistrations/` | **no** |
| How to run/score it | `docs/runbooks/` | yes |
| Code-side frozen constants | the experiment's `spec.py` + its pin test | only by an approved amendment |
| Design options under debate | `docs/superpowers/specs/` | yes, until decided |

A code pin (`PINNED_SPEC_SHA256`-style) and a file here must agree. If they
disagree, **the file here wins as the statement of intent** and the code pin is
the bug — but neither may be changed to match the other without a dated
amendment recorded in this directory.

## Index

| Date | Experiment | Status |
| --- | --- | --- |
| 2026-08-22 | [`support_strength_two_source_equivalence`](2026-08-22-support-strength-two-source-equivalence.md) (§139차 ②, retro §7-1) | registered; **collection not started** — blockers in §7 of that file |

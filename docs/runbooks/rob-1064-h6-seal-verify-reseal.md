# ROB-1064 H6 Trial-Accounting Seal — Verify / Reseal Runbook

Owner: Research (Alpaca track)
Related issues: ROB-1064 (H6 accounting), ROB-1062 (H4 terminal execution), ROB-1063 AC-7 (H5 one-shot)

How to verify an H6 trial-accounting seal, and how a future reseal must be produced
without deleting or overwriting an existing one. There is no CLI for the H6 seal
itself; the exact commands below are the interface.

## Scope and safety boundary

Everything in this runbook is count/status only. Nothing here reads live market
tape, touches a broker, registers a scheduler, or writes to a database.

Hard rules:

- **Never delete, truncate, or overwrite an existing seal.** Seals are append-only
  historical records. Both writers (`authority.materialize_materialized_seal`,
  `terminal_status.materialize_terminal_execution_artifact`) use exclusive-create
  and raise `FileExistsError` rather than replacing a file. Do not work around that.
- **Never compute forward return, PnL, or directional hit rate here (CRS-24).**
  This stage counts cells and reports status. A seal that carries a performance
  surface is a defect, pinned by `test_terminal_artifact_contains_no_performance_surface`.
- **Do not run H5.** H5's dry count is one-shot and irreversible (ROB-1063 AC-7).
  Passing this runbook is a precondition for H5, not an authorization for it.
  Order: reseal → independent verification (separate session) → H5 one-shot
  (separate approval) → unmask (separate approval).
- **Never relax a gate to make a seal pass.** `structural_incomplete`,
  `degenerate_fold_replication`, and the provenance interlock are fail-closed by
  design. A seal that cannot pass them is the answer, not an obstacle.

## 1. Seal inventory

`research/alpaca_track_accounting/sealed_reports/` — three seals, all git-tracked,
all preserved:

| file | sha256 | semantic_hash | state it proves |
| --- | --- | --- | --- |
| `rob-1064-current.json` | `3f8f8c67…` | `b57a600c…` | Pre-materialization (PR #1713). No H4 terminal evidence existed: all 128 cells `structural_incomplete`, `structural_incomplete == 16` trials, `performance_usable == false`, every `observation_count` explicitly `null` with a reason. Truthful emptiness, not a zero default. |
| `rob-1064-run-2026-07-29-h4-terminal-v1.json` | `95be02e9…` | `6176fe6b…` | First materialized seal (PR #1715). Counts arithmetically clean, informationally empty: H4 v1's corpus restarted one price path at every fold, so 128 cells carried 16 distinct observations replicated eight times. Preserved as evidence of that state; **not** regenerable. |
| `rob-1064-run-2026-07-29-h4-terminal-v2.json` | `ffa06be7…` | `98bf4cfe…` | Current seal (PR #1720), built from H4 identity `rob1062-h4-synthetic-ac27-v2` whose eight folds observe eight different periods. `structural_incomplete == 0`, `performance_usable == true`. |

The two superseded seals are pinned by content digest in
`authority.PRESERVED_SEAL_DIGESTS`, and `test_every_earlier_seal_is_preserved_byte_for_byte`
also pins the directory to exactly these three filenames. Adding a fourth file
fails that test until the map is updated deliberately.

The matching H4 evidence lives in
`research/alpaca_track_walkforward/terminal_artifacts/`: `…-v1.json` (superseded,
kept on disk, and deliberately **rejected** by the loader) and `…-v2.json` (current).

## 2. The two conditions — both are required

A seal is only meaningful if **both** hold:

1. `structural_incomplete == 0` and `performance_usable == true`.
2. The eight walk-forward folds actually observed **different** data.

Condition 2 is the one PR #1715 passed while being empty. Count checks cannot
detect its failure:

- `observation_count` is fixed by the decision calendar and is legitimately equal
  across folds of equal length. Measured on the current artifact, every config has
  exactly **1** distinct `observation_count` across its 8 folds. Any check built on
  it is vacuous by construction.
- `cells == 128`, `status_sum == 16`, `violations == []` were all true of the
  degenerate v1 artifact.

So condition 2 must be measured on the **full** blind-count payload, per config,
with `fold_id` removed. That is what `terminal_status._assert_folds_are_not_replicas`
enforces (fail-closed on both build and load) and what step 3C measures independently.

**Never report a seal as verified on condition 1 alone.**

## 3. Verification procedure (read-only)

Run from a worktree at the commit under review. Nothing below writes into the repo.

### 3A. Contract tests

```bash
cd /Users/mgh3326/work/auto_trader.<worktree>
uv run pytest research/alpaca_track_walkforward/tests/ -q --strict-markers -ra
uv run pytest research/alpaca_track_accounting/tests/ -q --strict-markers -ra
```

Both must be all-pass. These are the same invocations CI uses
(`.github/workflows/test.yml`). The H4 suite executes the full run and takes
several minutes. `make test` does **not** cover either package
(`pyproject.toml` `testpaths = ["tests"]`), so running them explicitly is required.

### 3B. Seal report, byte-exact regeneration, preservation digests

```bash
uv run python - <<'PY'
import hashlib, json, sys
from pathlib import Path
ROOT = Path.cwd()
for name in ("alpaca_track_accounting", "nautilus_scalping", "alpaca_track",
             "alpaca_track_seal", "alpaca_track_signals", "alpaca_track_walkforward"):
    sys.path.insert(0, str(ROOT / "research" / name))
sys.path.insert(0, str(ROOT))
import accounting as acct
import authority
seal = authority.build_materialized_seal()
committed = authority.MATERIALIZED_SEAL_PATH.read_bytes()
print("seal file          :", authority.MATERIALIZED_SEAL_PATH.name)
print("semantic_hash      :", seal.semantic_hash)
print("file sha256        :", hashlib.sha256(committed).hexdigest())
print("byte-exact rebuild :", seal.to_bytes() == committed)
print("report             :", json.dumps(json.loads(committed)["report"], sort_keys=True))
print("h5 gate usable     :", acct.verify_seal_for_h5(seal).performance_usable)
for name, digest in sorted(authority.PRESERVED_SEAL_DIGESTS.items()):
    actual = hashlib.sha256((authority._SEALED_REPORTS_DIR / name).read_bytes()).hexdigest()
    print(f"preserved {'OK   ' if actual == digest else 'DRIFT'} {name} {actual}")
PY
```

Expected for the current seal: `byte-exact rebuild: True`, `structural_incomplete: 0`,
`performance_usable: true`, `violations: []`, and `preserved OK` for both superseded
seals. `verify_seal_for_h5` only *verifies*; it does not run H5.

### 3C. Condition 2, measured independently of the repository's own gate

Reads committed artifact bytes with stdlib only, so a bug in
`_assert_folds_are_not_replicas` cannot make this pass:

```bash
uv run python - <<'PY'
import hashlib, json
from collections import defaultdict
from pathlib import Path
def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
for path in sorted(Path("research/alpaca_track_walkforward/terminal_artifacts").glob("*.json")):
    cells = json.loads(path.read_bytes())["cells"]
    per_config, obs = defaultdict(set), defaultdict(set)
    for c in cells:
        per_config[(c["family"], c["config_id"])].add(
            digest({k: v for k, v in c.items() if k != "fold_id"}))
        obs[(c["family"], c["config_id"])].add(c["observation_count"])
    payloads = {d for v in per_config.values() for d in v}
    print(f"{path.name}: cells={len(cells)} distinct_payloads={len(payloads)} "
          f"per_config_distinct={sorted({len(v) for v in per_config.values()})} "
          f"per_config_distinct_observation_count={sorted({len(v) for v in obs.values()})}")
PY
```

Expected:

```
rob1062-h4-synthetic-ac27-v1.json: cells=128 distinct_payloads=16  per_config_distinct=[1] per_config_distinct_observation_count=[1]
rob1062-h4-synthetic-ac27-v2.json: cells=128 distinct_payloads=128 per_config_distinct=[8] per_config_distinct_observation_count=[1]
```

Read it as three facts: the current artifact has 8/8 distinct fold payloads for
every config (128 distinct across 128 cells); the superseded v1 row proves the
same measurement *can* fail, so a `[8]` result is not a tautology; and the
`observation_count` column being `[1]` in both rows is why condition 2 cannot be
checked with counts.

Corroborating evidence, independent of the artifact:

- Fold windows: `fold_schedule.build_fold_schedule(...)` must give 8 folds, OOS
  windows non-overlapping, rolling forward exactly 28 days, 7-day embargo, and 8
  distinct TRAIN starts. Pinned by `tests/test_fold_boundary_invariants.py` and
  `test_fold_windows_do_not_overlap_in_oos_and_roll_forward`.
- Corpus: each fold must slice a **distinct** span of ONE absolute-time history,
  and any calendar day shared by two folds must price identically in both
  (`synthetic_corpus.absolute_day_index`; `tests/test_synthetic_corpus_single_history.py`).
  Both properties are required: distinct spans without the single-history
  invariant is exactly the v1 defect.

### 3D. Full re-execution (strongest check, ~7 min)

Reproduces the H4 artifact from source into a temp path and byte-compares. Never
point `--output` at the repository.

The sibling research packages import flat, so `PYTHONPATH` is required — without it
the CLI fails with `ModuleNotFoundError: No module named 'reason_codes'`:

```bash
OUT=$(mktemp -t h4-reexec-XXXX).json && rm -f "$OUT"
PYTHONPATH="research/alpaca_track_signals:research/alpaca_track_seal:research/alpaca_track:research/nautilus_scalping" \
  uv run python research/alpaca_track_walkforward/terminal_status.py --output "$OUT"
shasum -a 256 "$OUT" research/alpaca_track_walkforward/terminal_artifacts/rob1062-h4-synthetic-ac27-v2.json
```

Expected CLI line: `"cells": 128, "status_counts": {"executed": 128, "structural_incomplete": 0}`,
and the two digests must match. A mismatch means the sealed artifact is not the
output of the committed code — stop and report, do not reseal over it.

## 4. Reseal procedure (only when the sealed evidence is actually invalid)

A reseal is warranted when the sealed artifact no longer reflects the H3/H4
execution code, or when a defect like the v1 fold replication is found. It is
**not** warranted merely to produce a newer file: an identity bump that leaves the
counts unchanged manufactures an artifact without adding evidence.

`code_hash` is computed over the 24 files in `terminal_status._EXECUTION_SOURCE_PATHS`
(which include `terminal_status.py` and `synthetic_corpus.py` themselves). So *any*
edit to H3/H4 execution code invalidates the committed artifact: the loader's
provenance check rejects it and `build_materialized_seal` raises `AuthorityError`.
There is no partial reseal — a code change forces a whole new identity.

Steps, in order:

1. Fix the root cause in H3/H4 execution code. Add a fail-closed gate that would
   have caught the defect, and a test proving the gate is non-vacuous (i.e. that it
   rejects the defective shape).
2. Bump `run_manifest.CANONICAL_RUN_ID` to the next identity (`…-v3`).
3. Append the outgoing artifact path to `terminal_status.HISTORICAL_TERMINAL_ARTIFACT_PATHS`
   and point `CANONICAL_TERMINAL_ARTIFACT_PATH` at the new filename. Keep the old
   file on disk; the loader must keep rejecting it.
4. Regenerate the H4 artifact (`PYTHONPATH` as in section 3D):
   `PYTHONPATH="research/alpaca_track_signals:research/alpaca_track_seal:research/alpaca_track:research/nautilus_scalping" uv run python research/alpaca_track_walkforward/terminal_status.py --output research/alpaca_track_walkforward/terminal_artifacts/<new-identity>.json`
   (exclusive-create; it will refuse an existing path).
5. Add the outgoing seal's sha256 to `authority.PRESERVED_SEAL_DIGESTS` and point
   `authority.MATERIALIZED_SEAL_PATH` at a **new** seal filename.
6. Write the new seal via `authority.materialize_materialized_seal()` — new file only.
7. Update the pinned expectations in
   `research/alpaca_track_accounting/tests/test_authority_and_safety.py`
   (preserved-digest set, sealed_reports filename set, expected `run_id`) and in
   `research/alpaca_track_walkforward/tests/test_terminal_status.py`.
8. Re-run section 3 in full and report both conditions with evidence.

Regenerate the digests pinned in step 5 with `shasum -a 256 <file>`; never
hand-edit a digest to match a file you changed.

## 5. H5 entry preconditions

H5 may be considered only when all of these hold. Meeting them is not approval.

- Section 3A: both suites all-pass.
- Section 3B: `structural_incomplete == 0`, `performance_usable == true`,
  `violations == []`, byte-exact rebuild, both superseded seals `preserved OK`.
- Section 3C: `per_config_distinct == [8]` and `distinct_payloads == 128` for the
  current artifact.
- Section 3D: re-executed artifact byte-identical to the sealed one.
- An independent verification session (not the session that produced the seal) has
  reproduced the above.
- Explicit operator approval for the H5 one-shot, recorded separately.

## 6. Known limitation

`_assert_folds_are_not_replicas` rejects a config only when **all** of its folds
share one payload digest (`len(digests) < 2`). That is the exact v1 shape. A
partially degenerate artifact — say seven identical folds and one different — would
pass the gate. Today the stronger property (8/8 distinct for all 16 configs) is
pinned only by tests against the committed artifact, not by the runtime gate.
Tightening the gate is a fail-closed change, but it edits `terminal_status.py` and
therefore forces a new H4 identity and a reseal (section 4), so it should be
bundled with a reseal that is warranted on its own merits rather than done alone.

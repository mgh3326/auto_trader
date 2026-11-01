# U7-KR boosting baseline + frozen controls (ROB-1441)

A tuned LightGBM baseline for `H-admit` and `H-entry-price` on the U2 causal KR
dataset, evaluated through the U5 contract against frozen controls. If it fails
its gate, the ML track stops for that hypothesis and U8 TFT does not run.
Research only: no broker, account, database, scheduler or `app` import.

## Commands

The ML dependencies live in `requirements-ml.txt`, outside `pyproject.toml`.

```bash
uv venv --python 3.13 "$ROOT/venv"
VIRTUAL_ENV="$ROOT/venv" uv pip install -r research/kr_boosting/requirements-ml.txt

"$ROOT/venv/bin/python" -m research.kr_boosting.run validate   --export-dir "$EXPORT" --root "$ROOT/run"
"$ROOT/venv/bin/python" -m research.kr_boosting.run diagnostic --export-dir "$EXPORT" --root "$ROOT/run"
"$ROOT/venv/bin/python" -m research.kr_boosting.run report     --export-dir "$EXPORT" --root "$ROOT/run" \
  --report research/experiments/reports/rob-1441-kr-boosting-baseline.md
```

| Stage | Reads | Writes |
|---|---|---|
| `validate` | sessions through 2022 | nested fits, gate verdict, frozen models |
| `diagnostic` | sessions through 2024, frozen models only; refuses a second run | diagnostics, never the verdict |
| `report` | the two stages' JSON and the registry | the report, plus one rendered contract report per policy |

Run output stays in `$ROOT`, outside the repository. Each fit is cached under
`$ROOT/run/fit_cache`, keyed by its inputs, `train.py` and the LightGBM version, so
a crash or a simulator-only fix does not refit. The log has one line per fit.

## Modules

| Module | Role |
|---|---|
| `config.py` | Frozen literals: dates, frame, grid, gate |
| `panel.py` | Hash-checked U2 load; refuses 2023+ unless the diagnostic stage asks |
| `features.py` | 60-session features; `feature_join` refuses `available_at` after the decision time |
| `eligibility.py` | The D3 signal, vectorised; a test proves it equals the D3 engine |
| `labels.py` | `ret_open1_close5`, `touch_5`, `xret_20_given_touch`; unknown stays NaN |
| `splits.py` | Walk-forward folds with purge + gap at each boundary |
| `policies.py` | Candidate lists for the model and the frozen controls |
| `simulator.py` | Daily portfolio that emits U5 contract rows through U5 fill models |
| `gate.py` | Gate verdict: block bootstrap, Holm, stability, bracket |
| `train.py` | LightGBM, imported lazily; nested configuration selection |
| `pipeline.py`, `run.py`, `report.py` | Rows and runs, CLI, report |
| `exposure_audit.py` | Repository evidence that 2023-2024 was already read |
| `payout_exposure.py` | How much of each policy's result settled at a last close, split by the ROB-1437 audit's caveats |

The report and its verdict are in
`research/experiments/reports/rob-1441-kr-boosting-baseline.md`.

Why not the U5 KR adapter: it wraps the D3 engine, which ranks its own signals
and has no volume, so it can run neither a custom candidate list nor
`VolumeShare`. `simulator.py` uses the same contract types and fill models, and
a test pins its daily fill context to the adapter's.

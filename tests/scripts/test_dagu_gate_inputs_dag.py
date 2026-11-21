"""Static check for the T3 regime-gate input DAG proposal (hk 5889).

``deploy/dagu/at-gate-inputs/at-t3-crypto-gate-inputs.yaml`` is a PROPOSAL copy of
the registered-pattern crypto insight job with an explicit provider list. Nothing
registers it. This test pins the schedule to the at-batch1 slot, the one allowed
command shape, the provider set (G1-G4 inputs), the default job being unchanged,
and the journal-then-optional-ping handlers. It does not call Dagu, Docker or a
database.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from app.services.crypto_insight_snapshots.builder import (
    DEFAULT_PROVIDERS,
    GATE_INPUT_PROVIDERS,
    _provider_fetcher,
)

ROOT = Path(__file__).resolve().parents[2]
DAG_PATH = ROOT / "deploy/dagu/at-gate-inputs/at-t3-crypto-gate-inputs.yaml"
AT_BATCH1 = ROOT / "deploy/dagu/at-batch/at-batch1-crypto-insight.yaml"
JOURNAL_TAG = "at-dagu-batch"
PING = "AT_BATCH_PING_URL_CRYPTO_GATE_INPUTS"
G_INPUTS = {
    "alternative_me",
    "defillama",
    "binance",
    "upbit_binance",
    "binance_settlement",
}

COMMAND_RE = re.compile(
    r"docker run --rm --network host --workdir /app --env-file /root/at-secrets/\.env\.api "
    r"-e '(CRYPTO_INSIGHT_PROVIDERS_JSON=\[[^']*\])' "
    r'"\$\(cat /root/at-run/deployed-digest\)" /app/\.venv/bin/python '
    r"-m scripts\.build_crypto_insight_snapshots"
)
FORBIDDEN = (
    " -v ",
    " --volume",
    " --mount",
    " --privileged",
    " --entrypoint",
    " --pid",
    " sudo ",
    "&&",
    "||",
    "|&",
    ";",
    "|",
    "`",
    "--commit",
    "CRYPTO_INSIGHT_COMMIT_ENABLED",
    "invest_crypto_insight_snapshots",
)


def _load() -> tuple[str, dict]:
    text = DAG_PATH.read_text()
    return text, yaml.safe_load(text)


def test_only_this_one_proposal_file_exists_in_its_directory():
    assert sorted(p.name for p in DAG_PATH.parent.glob("*.yaml")) == [DAG_PATH.name]


def test_proposal_is_marked_unregistered_in_description_and_header():
    text, dag = _load()
    assert dag["description"].startswith("PROPOSAL")
    assert "미등록" in dag["description"]
    assert "어디에도 등록되어 있지 않다" in text


def test_schedule_is_the_registered_at_batch1_crypto_insight_slot():
    _, dag = _load()
    registered = yaml.safe_load(AT_BATCH1.read_text())
    assert dag["schedule"] == registered["schedule"] == "CRON_TZ=UTC 50 0 * * *"


def test_worker_selector_dotenv_and_single_flight_match_the_pattern():
    _, dag = _load()
    assert dag["worker_selector"] == {"host": "pi"}
    assert dag["dotenv"] == "/root/at-secrets/.env.dagu-at-batch"
    assert dag["max_active_runs"] == 1
    assert dag["hist_retention_days"] == 60


def test_single_build_step_is_the_allowlisted_container_form():
    _, dag = _load()
    assert [s["name"] for s in dag["steps"]] == ["build"]
    step = dag["steps"][0]
    assert step["timeout_sec"] == 3600
    assert step["retry_policy"] == {"limit": 1, "interval_sec": 120}
    match = COMMAND_RE.fullmatch(step["command"])
    assert match, step["command"]
    for token in FORBIDDEN:
        assert token not in step["command"], token
    assert (ROOT / "scripts/build_crypto_insight_snapshots.py").is_file()


def test_explicit_provider_list_covers_g1_to_g4_and_every_name_is_supported():
    _, dag = _load()
    raw = COMMAND_RE.fullmatch(dag["steps"][0]["command"]).group(1)
    providers = json.loads(raw.split("=", 1)[1])
    assert len(providers) == len(set(providers))
    assert G_INPUTS <= set(providers)
    assert set(GATE_INPUT_PROVIDERS) <= set(providers)
    for name in providers:
        # Only the name dispatch is checked; no provider is called here.
        assert callable(_provider_fetcher(name, symbols=(), now=None)), name


def test_default_job_provider_set_is_unchanged_by_the_gate_inputs():
    assert DEFAULT_PROVIDERS == ("alternative_me", "coingecko", "binance")
    assert not set(GATE_INPUT_PROVIDERS) & set(DEFAULT_PROVIDERS)


def test_handlers_are_one_journal_line_then_an_optional_ping():
    _, dag = _load()
    success = (
        f'logger -t {JOURNAL_TAG} "dag=at-t3-crypto-gate-inputs status=success"; '
        f'[ -z "${{{PING}:-}}" ] || curl -fsS -m 10 --retry 3 -o /dev/null "${{{PING}}}"'
    )
    failure = (
        f'logger -t {JOURNAL_TAG} "dag=at-t3-crypto-gate-inputs status=failure"; '
        f'[ -z "${{{PING}:-}}" ] || curl -fsS -m 10 --retry 3 -o /dev/null "${{{PING}}}/fail"'
    )
    assert dag["handler_on"]["success"]["command"] == success
    assert dag["handler_on"]["failure"]["command"] == failure


def test_no_ping_url_value_is_committed():
    text, _ = _load()
    assert not re.search(r"https?://", text)

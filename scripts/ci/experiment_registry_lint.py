"""Experiment registry lint (ROB-1429) — stdlib only, fail closed.

Checks, against the repository checkout:

1. ``research/experiments/registry.json`` matches the registry schema
   (closed keys, types, enums, unique ids).
2. Every registered manifest exists, matches the manifest schema, names its
   hypothesis, and hashes to the registered ``manifest_sha256``.
3. ``k`` equals the number of ``test_looks``; looks are numbered 1..k, each
   cites a hash the hypothesis has registered, and each look's report exists,
   cites that hash and declares ``Experiment-Test-Look: yes``.
4. Every report under the scanned roots that cites a manifest hash
   (``Experiment-Manifest-Sha256: <hex>``) cites a registered hash; every
   report that declares a test look is counted in the registry. Every file in
   ``research/experiments/reports/`` must cite a hash.
5. ``research/experiments/heartbeat.json`` matches its schema, and every
   hypothesis whose status is ``running`` has a heartbeat epoch.
6. With ``--base-registry``: the registry only grows. No hypothesis is
   removed, ``k`` never decreases, earlier ``test_looks`` stay as a prefix,
   and a replaced manifest hash is kept in ``previous_manifest_sha256``.

Any error exits 1 with one line per violation. There is no warning level.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path("research/experiments/registry.json")
HEARTBEAT_PATH = Path("research/experiments/heartbeat.json")
REPORTS_DIR = Path("research/experiments/reports")
REPORT_SCAN_ROOTS: tuple[Path, ...] = (Path("research"), Path("docs/research"))
SPLIT_RULES_DOC = "docs/research/experiment-split-leakage-rules.md"

MANIFEST_FORMAT = "experiment-manifest/v1"
REGISTRY_SCHEMA_VERSION = 1
HEARTBEAT_SCHEMA_VERSION = 1
MIN_GAP_SESSIONS = 5

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A marker is the key followed by ":" or a table "|" anywhere on a line, in any
# case and under any markdown decoration (bold, list, quote, table, indent).
# Prose that names a key without ":" or "|" after it is not a marker.
CITE_RE = re.compile(
    r"experiment-manifest-sha256[\s*_`]*[:|][\s*_`|]*([^\s*_`|]*)", re.I
)
LOOK_RE = re.compile(r"experiment-test-look[\s*_`]*[:|][\s*_`|]*([^\s*_`|]*)", re.I)
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

MARKETS = frozenset({"kr", "us", "crypto"})
HYPOTHESIS_STATUSES = frozenset({"draft", "sealed", "running", "closed", "retired"})
MANIFEST_STATUSES = frozenset({"draft", "sealed"})
CALENDARS = frozenset({"XKRX", "XNYS", "24/7"})
TEST_KINDS = frozenset({"sealed_period", "prospective_seal"})
SOURCE_KINDS = frozenset({"hash_chained_ledger", "json_timestamps"})

REGISTRY_KEYS = frozenset({"schema_version", "hypotheses"})
HYPOTHESIS_KEYS = frozenset(
    {
        "id",
        "title",
        "tracker",
        "markets",
        "status",
        "registered_at",
        "manifest",
        "manifest_sha256",
        "previous_manifest_sha256",
        "k",
        "test_looks",
    }
)
LOOK_KEYS = frozenset({"seq", "at", "manifest_sha256", "report", "report_sha256"})
STATUS_ORDER = {"draft": 0, "sealed": 1, "running": 2, "closed": 3}

MANIFEST_KEYS = frozenset(
    {
        "manifest_format",
        "hypothesis_id",
        "version",
        "title",
        "status",
        "question",
        "markets",
        "hashes",
        "features",
        "labels",
        "calendar",
        "splits",
        "candidate_policies",
        "costs_fills",
        "tuning_budget",
        "seeds",
        "metrics",
        "stopping_rules",
        "cumulative_k",
    }
)
# Sections a draft manifest may leave as {"pending": "<reason>"}.
PENDABLE_SECTIONS = frozenset(
    {
        "hashes",
        "features",
        "labels",
        "calendar",
        "splits",
        "candidate_policies",
        "costs_fills",
        "tuning_budget",
        "seeds",
        "metrics",
        "stopping_rules",
    }
)
SPLIT_KEYS = frozenset(
    {"scheme", "rules_doc", "purge_sessions", "gap_sessions", "folds", "test"}
)
TEST_KEYS = frozenset({"kind", "period", "exposure_audit", "seal_rule"})
FOLD_KEYS = frozenset({"train", "validate"})
FEATURE_KEYS = frozenset({"name", "available_at", "source"})
LABEL_KEYS = frozenset({"name", "definition", "horizon_sessions"})
POLICY_KEYS = frozenset({"id", "description"})
COSTS_KEYS = frozenset({"cost_table", "fill_models"})
TUNING_KEYS = frozenset({"max_configurations", "search", "notes"})
METRICS_KEYS = frozenset({"primary", "secondary", "family", "alpha", "delta_min"})
CUMK_KEYS = frozenset({"prior_k", "source"})

HEARTBEAT_KEYS = frozenset({"schema_version", "idle_red_after_sessions", "epochs"})
EPOCH_KEYS = frozenset({"id", "hypothesis", "description", "calendar", "source"})
SOURCE_KEYS = frozenset({"kind", "path", "advance_kinds", "select"})


class Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.items.append(f"{where}: {message}")


def _is_date(value: Any) -> bool:
    """A real calendar date written as YYYY-MM-DD (2026-02-30 is rejected)."""

    if not (isinstance(value, str) and DATE_RE.match(value)):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _member(value: Any, allowed: frozenset[str] | dict[str, Any]) -> bool:
    return isinstance(value, str) and value in allowed


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def canonical_sha256(obj: Any) -> str:
    """sha256 of the canonical JSON text of a parsed manifest."""

    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _load_json(path: Path, where: str, errors: Errors) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.add(where, f"cannot read {path} ({exc.__class__.__name__})")
        return None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        errors.add(where, f"invalid JSON at line {exc.lineno}")
        return None
    except ValueError as exc:
        errors.add(where, f"invalid JSON: {exc}")
        return None


def _closed_keys(
    obj: Any, required: frozenset[str], where: str, errors: Errors, *, optional=()
) -> bool:
    if not isinstance(obj, dict):
        errors.add(where, "must be an object")
        return False
    allowed = required | frozenset(optional)
    for key in sorted(set(obj) - allowed):
        errors.add(where, f"unknown key {key!r}")
    for key in sorted(required - set(obj)):
        errors.add(where, f"missing key {key!r}")
    return True


def _markets(value: Any, where: str, errors: Errors) -> None:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(m, str) for m in value)
        or len(set(value)) != len(value)
        or not set(value) <= MARKETS
    ):
        errors.add(
            where, f"markets must be a non-empty unique subset of {sorted(MARKETS)}"
        )


def _str_list(value: Any, where: str, errors: Errors, *, allow_empty=False) -> None:
    if not isinstance(value, list) or not all(_is_str(v) for v in value):
        errors.add(where, "must be a list of non-empty strings")
    elif not value and not allow_empty:
        errors.add(where, "must not be empty")


def _contains_pending(obj: Any) -> bool:
    if isinstance(obj, dict):
        if set(obj) == {"pending"}:
            return True
        return any(_contains_pending(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_pending(v) for v in obj)
    return False


def _is_pending(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"pending"}
        and _is_str(value["pending"])
    )


# --------------------------------------------------------------- manifest
def validate_manifest(manifest: Any, where: str, errors: Errors) -> None:
    if not _closed_keys(manifest, MANIFEST_KEYS, where, errors):
        return
    if manifest.get("manifest_format") != MANIFEST_FORMAT:
        errors.add(where, f"manifest_format must be {MANIFEST_FORMAT!r}")
    if not _is_str(manifest.get("hypothesis_id")) or not ID_RE.match(
        manifest.get("hypothesis_id", "")
    ):
        errors.add(where, "hypothesis_id must match the id pattern")
    if not _is_int(manifest.get("version")) or manifest.get("version", 0) < 1:
        errors.add(where, "version must be an integer >= 1")
    for key in ("title", "question"):
        if not _is_str(manifest.get(key)):
            errors.add(where, f"{key} must be a non-empty string")
    status = manifest.get("status")
    if not _member(status, MANIFEST_STATUSES):
        errors.add(where, f"status must be one of {sorted(MANIFEST_STATUSES)}")
    if "markets" in manifest:
        _markets(manifest["markets"], f"{where}.markets", errors)

    for key in sorted(PENDABLE_SECTIONS & set(manifest)):
        value = manifest[key]
        if _is_pending(value):
            continue
        if isinstance(value, dict) and "pending" in value and len(value) == 1:
            errors.add(f"{where}.{key}", "pending reason must be a non-empty string")
            continue
        _validate_section(key, value, f"{where}.{key}", errors)

    if status == "sealed" and _contains_pending(manifest):
        errors.add(where, "a sealed manifest must not contain any pending value")

    cum = manifest.get("cumulative_k")
    if _closed_keys(cum, CUMK_KEYS, f"{where}.cumulative_k", errors):
        if not _is_int(cum.get("prior_k")) or cum.get("prior_k", -1) < 0:
            errors.add(f"{where}.cumulative_k", "prior_k must be an integer >= 0")
        if cum.get("source") != "registry":
            errors.add(f"{where}.cumulative_k", "source must be 'registry'")

    labels = manifest.get("labels")
    splits = manifest.get("splits")
    if (
        isinstance(labels, list)
        and isinstance(splits, dict)
        and not _is_pending(splits)
    ):
        horizons = [
            lab.get("horizon_sessions")
            for lab in labels
            if isinstance(lab, dict) and _is_int(lab.get("horizon_sessions"))
        ]
        purge = splits.get("purge_sessions")
        if horizons and _is_int(purge) and purge < max(horizons):
            errors.add(
                f"{where}.splits",
                f"purge_sessions {purge} is below the longest label horizon {max(horizons)}",
            )
        gap = splits.get("gap_sessions")
        if horizons and _is_int(gap) and gap < max(MIN_GAP_SESSIONS, *horizons):
            errors.add(
                f"{where}.splits",
                f"gap_sessions {gap} is below max({MIN_GAP_SESSIONS}, longest label "
                f"horizon {max(horizons)}) (split rules section 4)",
            )


def _validate_section(key: str, value: Any, where: str, errors: Errors) -> None:
    if key == "hashes":
        if not isinstance(value, dict) or not value:
            errors.add(where, "must be a non-empty object")
            return
        for name, item in value.items():
            if not (_is_str(item) or _is_pending(item)):
                errors.add(f"{where}.{name}", "must be a string or a pending object")
    elif key == "features":
        if not isinstance(value, list) or not value:
            errors.add(where, "must be a non-empty list")
            return
        for i, item in enumerate(value):
            w = f"{where}[{i}]"
            if _closed_keys(
                item, FEATURE_KEYS - {"source"}, w, errors, optional={"source"}
            ):
                for k in ("name", "available_at"):
                    if not _is_str(item.get(k)):
                        errors.add(w, f"{k} must be a non-empty string")
    elif key == "labels":
        if not isinstance(value, list) or not value:
            errors.add(where, "must be a non-empty list")
            return
        for i, item in enumerate(value):
            w = f"{where}[{i}]"
            if _closed_keys(item, LABEL_KEYS, w, errors):
                for k in ("name", "definition"):
                    if not _is_str(item.get(k)):
                        errors.add(w, f"{k} must be a non-empty string")
                h = item.get("horizon_sessions")
                if not _is_int(h) or h < 1:
                    errors.add(w, "horizon_sessions must be an integer >= 1")
    elif key == "calendar":
        if not isinstance(value, dict) or not value:
            errors.add(where, "must map each market to a calendar")
            return
        for market, cal in value.items():
            if market not in MARKETS:
                errors.add(where, f"unknown market {market!r}")
            if not _member(cal, CALENDARS):
                errors.add(
                    f"{where}.{market}", f"calendar must be one of {sorted(CALENDARS)}"
                )
    elif key == "splits":
        _validate_splits(value, where, errors)
    elif key == "candidate_policies":
        if not isinstance(value, list) or not value:
            errors.add(where, "must be a non-empty list")
            return
        ids: set[str] = set()
        for i, item in enumerate(value):
            w = f"{where}[{i}]"
            if _closed_keys(item, POLICY_KEYS, w, errors):
                pid = item.get("id")
                if not _is_str(pid):
                    errors.add(w, "id must be a non-empty string")
                elif pid in ids:
                    errors.add(w, f"duplicate policy id {pid!r}")
                else:
                    ids.add(pid)
                if not _is_str(item.get("description")):
                    errors.add(w, "description must be a non-empty string")
    elif key == "costs_fills":
        if _closed_keys(value, COSTS_KEYS, where, errors):
            if not (
                _is_str(value.get("cost_table")) or _is_pending(value.get("cost_table"))
            ):
                errors.add(where, "cost_table must be a string or a pending object")
            _str_list(value.get("fill_models"), f"{where}.fill_models", errors)
    elif key == "tuning_budget":
        if _closed_keys(
            value, TUNING_KEYS - {"notes"}, where, errors, optional={"notes"}
        ):
            m = value.get("max_configurations")
            if not _is_int(m) or m < 0:
                errors.add(where, "max_configurations must be an integer >= 0")
            if not _is_str(value.get("search")):
                errors.add(where, "search must be a non-empty string")
    elif key == "seeds":
        if (
            not isinstance(value, list)
            or not value
            or not all(_is_int(s) for s in value)
        ):
            errors.add(where, "must be a non-empty list of integers")
    elif key == "metrics":
        if _closed_keys(value, METRICS_KEYS, where, errors):
            if not _is_str(value.get("primary")):
                errors.add(where, "primary must be a non-empty string")
            _str_list(
                value.get("secondary"), f"{where}.secondary", errors, allow_empty=True
            )
            _str_list(value.get("family"), f"{where}.family", errors)
            for k in ("alpha", "delta_min"):
                if not (_is_str(value.get(k)) or _is_pending(value.get(k))):
                    errors.add(
                        where, f"{k} must be a decimal string or a pending object"
                    )
    elif key == "stopping_rules":
        _str_list(value, where, errors)


def _validate_splits(value: Any, where: str, errors: Errors) -> None:
    if not _closed_keys(value, SPLIT_KEYS, where, errors):
        return
    if not _is_str(value.get("scheme")):
        errors.add(where, "scheme must be a non-empty string")
    if value.get("rules_doc") != SPLIT_RULES_DOC:
        errors.add(where, f"rules_doc must be {SPLIT_RULES_DOC!r}")
    purge = value.get("purge_sessions")
    if not _is_int(purge) or purge < 0:
        errors.add(where, "purge_sessions must be an integer >= 0")
    gap = value.get("gap_sessions")
    if not _is_int(gap) or gap < MIN_GAP_SESSIONS:
        errors.add(where, f"gap_sessions must be an integer >= {MIN_GAP_SESSIONS}")
    folds = value.get("folds")
    test = value.get("test")
    prospective = isinstance(test, dict) and test.get("kind") == "prospective_seal"
    if not isinstance(folds, list):
        errors.add(where, "folds must be a list")
    elif not folds and not prospective:
        errors.add(where, "folds may be empty only with a prospective_seal test")
    else:
        for i, fold in enumerate(folds):
            w = f"{where}.folds[{i}]"
            if _closed_keys(fold, FOLD_KEYS, w, errors):
                for k in ("train", "validate"):
                    if not _date_range(fold.get(k)):
                        errors.add(
                            w, f"{k} must be [YYYY-MM-DD, YYYY-MM-DD] with start <= end"
                        )
                if _date_range(fold.get("train")) and _date_range(fold.get("validate")):
                    if fold["train"][1] >= fold["validate"][0]:
                        errors.add(w, "train must end before validate starts")
    if _closed_keys(test, TEST_KEYS, f"{where}.test", errors):
        if not _member(test.get("kind"), TEST_KINDS):
            errors.add(f"{where}.test", f"kind must be one of {sorted(TEST_KINDS)}")
        period = test.get("period")
        if not (_date_range(period) or _is_pending(period)):
            errors.add(
                f"{where}.test", "period must be a date range or a pending object"
            )
        for k in ("exposure_audit", "seal_rule"):
            if not (_is_str(test.get(k)) or _is_pending(test.get(k))):
                errors.add(f"{where}.test", f"{k} must be a string or a pending object")
        if (
            isinstance(folds, list)
            and folds
            and _date_range(period)
            and all(
                isinstance(f, dict) and _date_range(f.get("validate")) for f in folds
            )
            and max(f["validate"][1] for f in folds) >= period[0]
        ):
            errors.add(
                f"{where}.test", "test period must start after every validation period"
            )


def _date_range(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(_is_date(d) for d in value)
        and value[0] <= value[1]
    )


# --------------------------------------------------------------- registry
def validate_registry(
    registry: Any, root: Path, errors: Errors
) -> dict[str, dict[str, Any]]:
    """Validate the registry and its manifests; return hypotheses by id."""

    where = str(REGISTRY_PATH)
    by_id: dict[str, dict[str, Any]] = {}
    if not _closed_keys(registry, REGISTRY_KEYS, where, errors):
        return by_id
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        errors.add(where, f"schema_version must be {REGISTRY_SCHEMA_VERSION}")
    hypotheses = registry.get("hypotheses")
    if not isinstance(hypotheses, list):
        errors.add(where, "hypotheses must be a list")
        return by_id
    for i, hyp in enumerate(hypotheses):
        w = f"{where}.hypotheses[{i}]"
        if not _closed_keys(hyp, HYPOTHESIS_KEYS, w, errors):
            continue
        hid = hyp.get("id")
        if not isinstance(hid, str) or not ID_RE.match(hid):
            errors.add(w, "id must match ^[A-Za-z][A-Za-z0-9._-]{0,63}$")
            continue
        if hid in by_id:
            errors.add(w, f"duplicate id {hid!r}")
            continue
        by_id[hid] = hyp
        _validate_hypothesis(hyp, f"{where}[{hid}]", root, errors)
    return by_id


def _validate_hypothesis(
    hyp: dict[str, Any], w: str, root: Path, errors: Errors
) -> None:
    if not _is_str(hyp.get("title")):
        errors.add(w, "title must be a non-empty string")
    tracker = hyp.get("tracker")
    if tracker is not None and not _is_str(tracker):
        errors.add(w, "tracker must be a non-empty string or null")
    _markets(hyp.get("markets"), f"{w}.markets", errors)
    if not _member(hyp.get("status"), HYPOTHESIS_STATUSES):
        errors.add(w, f"status must be one of {sorted(HYPOTHESIS_STATUSES)}")
    if not _is_date(hyp.get("registered_at")):
        errors.add(w, "registered_at must be a real date YYYY-MM-DD")
    sha = hyp.get("manifest_sha256")
    if not (isinstance(sha, str) and SHA_RE.match(sha)):
        errors.add(w, "manifest_sha256 must be 64 lowercase hex characters")
    previous = hyp.get("previous_manifest_sha256")
    if not isinstance(previous, list) or not all(
        isinstance(p, str) and SHA_RE.match(p) for p in previous
    ):
        errors.add(w, "previous_manifest_sha256 must be a list of sha256 hex strings")
        previous = []
    if (isinstance(sha, str) and sha in previous) or len(set(previous)) != len(
        previous
    ):
        errors.add(w, "manifest hashes must be unique across current and previous")

    k = hyp.get("k")
    looks = hyp.get("test_looks")
    if not _is_int(k) or k < 0:
        errors.add(w, "k must be an integer >= 0")
    if not isinstance(looks, list):
        errors.add(w, "test_looks must be a list")
        looks = []
    if _is_int(k) and k != len(looks):
        errors.add(
            w, f"k={k} but test_looks has {len(looks)} entries (each look increments k)"
        )
    known = {s for s in [sha, *previous] if isinstance(s, str)}
    for j, look in enumerate(looks, start=1):
        lw = f"{w}.test_looks[{j - 1}]"
        if not _closed_keys(look, LOOK_KEYS, lw, errors):
            continue
        if look.get("seq") != j:
            errors.add(lw, f"seq must be {j}")
        if not _is_date(look.get("at")):
            errors.add(lw, "at must be a real date YYYY-MM-DD")
        if not _member(look.get("manifest_sha256"), known):
            errors.add(
                lw, "manifest_sha256 is not a hash registered for this hypothesis"
            )
        report = look.get("report")
        if not _is_str(report):
            errors.add(lw, "report must be a repository path")
            continue
        report_path = root / report
        if not report_path.is_file():
            errors.add(lw, f"report {report} does not exist")
            continue
        content = report_path.read_bytes()
        if look.get("report_sha256") != hashlib.sha256(content).hexdigest():
            errors.add(
                lw,
                f"report_sha256 does not match {report}; a look's report is frozen "
                "once counted (write a new report and a new look instead)",
            )
        markers = _report_markers(content.decode("utf-8", errors="replace"))
        cites, look_flag = markers.cites, markers.look
        if look.get("manifest_sha256") not in cites:
            errors.add(lw, f"report {report} does not cite the look's manifest hash")
        if look_flag != "yes":
            errors.add(
                lw, f"report {report} does not declare Experiment-Test-Look: yes"
            )

    manifest_rel = hyp.get("manifest")
    if not _is_str(manifest_rel):
        errors.add(w, "manifest must be a repository path")
        return
    manifest_path = root / manifest_rel
    manifest = _load_json(manifest_path, f"{w}.manifest", errors)
    if manifest is None:
        return
    validate_manifest(manifest, manifest_rel, errors)
    if isinstance(manifest, dict) and manifest.get("hypothesis_id") != hyp.get("id"):
        errors.add(
            w,
            f"manifest {manifest_rel} names hypothesis {manifest.get('hypothesis_id')!r}",
        )
    if isinstance(manifest, dict) and isinstance(manifest.get("markets"), list):
        if sorted(manifest["markets"]) != sorted(hyp.get("markets") or []):
            errors.add(w, "registry markets differ from the manifest markets")
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if _is_int(version) and version != len(previous) + 1:
        errors.add(
            w,
            f"manifest version {version} must be {len(previous) + 1} "
            "(1 + number of previous_manifest_sha256)",
        )
    prior_k = (
        manifest.get("cumulative_k", {}).get("prior_k")
        if isinstance(manifest, dict) and isinstance(manifest.get("cumulative_k"), dict)
        else None
    )
    earlier = sum(
        1
        for look in looks
        if isinstance(look, dict)
        and _member(look.get("manifest_sha256"), frozenset(previous))
    )
    if _is_int(prior_k) and prior_k != earlier:
        errors.add(
            w,
            f"cumulative_k.prior_k is {prior_k} but {earlier} test look(s) were "
            "recorded under previous manifest hashes",
        )
    actual = canonical_sha256(manifest)
    if actual != sha:
        errors.add(
            w,
            f"manifest {manifest_rel} hashes to {actual}; registered {sha}. "
            "A changed manifest is a new version: move the old hash to "
            "previous_manifest_sha256 and register the new one",
        )
    if _member(
        hyp.get("status"), frozenset({"sealed", "running", "closed"})
    ) and isinstance(manifest, dict):
        if manifest.get("status") != "sealed":
            errors.add(w, f"status {hyp.get('status')!r} requires a sealed manifest")


@dataclass(frozen=True)
class Markers:
    cites: frozenset[str]
    look: str | None
    look_count: int
    problems: tuple[str, ...]


def _report_markers(text: str) -> Markers:
    text = text.replace("\ufeff", "")
    problems: list[str] = []
    cites: set[str] = set()
    for raw in CITE_RE.findall(text):
        if HEX64_RE.match(raw):
            cites.add(raw.lower())
        else:
            problems.append(
                f"malformed Experiment-Manifest-Sha256 value {raw[:20]!r} "
                "(expected 64 hex characters)"
            )
    looks = [v.lower() for v in LOOK_RE.findall(text)]
    for value in looks:
        if value not in {"yes", "no"}:
            problems.append("Experiment-Test-Look must be 'yes' or 'no'")
    if len(looks) > 1:
        problems.append("Experiment-Test-Look is declared more than once")
    look = looks[0] if len(looks) == 1 and looks[0] in {"yes", "no"} else None
    if len(looks) > 1 and "yes" in looks:
        look = "yes"
    return Markers(frozenset(cites), look, len(looks), tuple(problems))


def iter_report_files(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for scan in REPORT_SCAN_ROOTS:
        base = root / scan
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path


def validate_reports(
    root: Path, by_id: dict[str, dict[str, Any]], errors: Errors
) -> None:
    registered: dict[str, str] = {}
    counted: set[tuple[str, str]] = set()
    for hid, hyp in by_id.items():
        for sha in [
            hyp.get("manifest_sha256"),
            *_as_list(hyp.get("previous_manifest_sha256")),
        ]:
            if isinstance(sha, str):
                registered[sha] = hid
        for look in _as_list(hyp.get("test_looks")):
            if isinstance(look, dict):
                counted.add((str(look.get("report")), str(look.get("manifest_sha256"))))

    reports_dir = root / REPORTS_DIR
    if reports_dir.is_dir():
        for path in sorted(reports_dir.rglob("*")):
            if path.is_file() and path.suffix != ".md":
                rel = path.relative_to(root).as_posix()
                errors.add(rel, "experiment reports must be .md files")
    for path in iter_report_files(root):
        rel = path.relative_to(root).as_posix()
        markers = _report_markers(path.read_text(encoding="utf-8", errors="replace"))
        cites, look_flag = markers.cites, markers.look
        for problem in markers.problems:
            errors.add(rel, problem)
        in_reports_dir = reports_dir in path.parents
        if in_reports_dir and path.name != "README.md" and not cites:
            errors.add(
                rel, "experiment report must cite Experiment-Manifest-Sha256: <hash>"
            )
        for sha in sorted(cites):
            if sha not in registered:
                errors.add(rel, f"cites unregistered manifest hash {sha}")
        if cites and markers.look_count == 0:
            errors.add(
                rel,
                "a report that cites a manifest hash must declare "
                "Experiment-Test-Look yes or no",
            )
        if look_flag == "yes":
            if not cites:
                errors.add(
                    rel, "a test look must cite the manifest hash it looked under"
                )
            for sha in sorted(cites):
                if sha in registered and (rel, sha) not in counted:
                    errors.add(
                        rel,
                        f"declares a test look under {registered[sha]} that the registry "
                        "does not count: append it to test_looks and increment k",
                    )


# --------------------------------------------------------------- heartbeat
def validate_heartbeat(
    config: Any, by_id: dict[str, dict[str, Any]], errors: Errors
) -> None:
    where = str(HEARTBEAT_PATH)
    if not _closed_keys(config, HEARTBEAT_KEYS, where, errors):
        return
    if config.get("schema_version") != HEARTBEAT_SCHEMA_VERSION:
        errors.add(where, f"schema_version must be {HEARTBEAT_SCHEMA_VERSION}")
    red = config.get("idle_red_after_sessions")
    if not _is_int(red) or red < 1:
        errors.add(where, "idle_red_after_sessions must be an integer >= 1")
    epochs = config.get("epochs")
    if not isinstance(epochs, list) or not epochs:
        errors.add(where, "epochs must be a non-empty list")
        return
    seen: set[str] = set()
    watched: set[str] = set()
    for i, epoch in enumerate(epochs):
        w = f"{where}.epochs[{i}]"
        if not _closed_keys(epoch, EPOCH_KEYS, w, errors):
            continue
        eid = epoch.get("id")
        if not isinstance(eid, str) or not ID_RE.match(eid):
            errors.add(w, "id must match the id pattern")
        elif eid in seen:
            errors.add(w, f"duplicate epoch id {eid!r}")
        else:
            seen.add(eid)
        hyp = epoch.get("hypothesis")
        if hyp is not None:
            if not _member(hyp, by_id):
                errors.add(w, f"hypothesis {hyp!r} is not registered")
            else:
                watched.add(hyp)
        if not _is_str(epoch.get("description")):
            errors.add(w, "description must be a non-empty string")
        if not _member(epoch.get("calendar"), CALENDARS):
            errors.add(w, f"calendar must be one of {sorted(CALENDARS)}")
        source = epoch.get("source")
        if not isinstance(source, dict):
            errors.add(f"{w}.source", "must be an object")
            continue
        kind = source.get("kind")
        if not _member(kind, SOURCE_KINDS):
            errors.add(f"{w}.source", f"kind must be one of {sorted(SOURCE_KINDS)}")
            continue
        extra = "advance_kinds" if kind == "hash_chained_ledger" else "select"
        if _closed_keys(
            source, frozenset({"kind", "path", extra}), f"{w}.source", errors
        ):
            path = source.get("path")
            if path is not None and not _is_str(path):
                errors.add(
                    f"{w}.source",
                    "path must be a string or null (supplied with --path)",
                )
            _str_list(source.get(extra), f"{w}.source.{extra}", errors)
    for hid, hyp in by_id.items():
        if hyp.get("status") == "running" and hid not in watched:
            errors.add(where, f"running hypothesis {hid!r} has no heartbeat epoch")


# --------------------------------------------------------------- base
def validate_against_base(
    base: Any, by_id: dict[str, dict[str, Any]], errors: Errors
) -> None:
    where = "base-registry"
    if not isinstance(base, dict) or not isinstance(base.get("hypotheses"), list):
        errors.add(where, "base registry is not a registry object")
        return
    for old in base["hypotheses"]:
        if not isinstance(old, dict):
            continue
        hid = old.get("id")
        new = by_id.get(hid) if isinstance(hid, str) else None
        w = f"{where}[{hid}]"
        if new is None:
            errors.add(w, "hypothesis was removed; set status 'retired' instead")
            continue
        old_k, new_k = old.get("k"), new.get("k")
        if _is_int(old_k) and _is_int(new_k) and new_k < old_k:
            errors.add(w, f"k decreased from {old_k} to {new_k}")
        old_looks = _as_list(old.get("test_looks"))
        new_looks = _as_list(new.get("test_looks"))
        if new_looks[: len(old_looks)] != old_looks:
            errors.add(w, "earlier test_looks were changed or removed (append only)")
        old_sha = old.get("manifest_sha256")
        new_known = {
            s
            for s in [
                new.get("manifest_sha256"),
                *_as_list(new.get("previous_manifest_sha256")),
            ]
            if isinstance(s, str)
        }
        for sha in [old_sha, *_as_list(old.get("previous_manifest_sha256"))]:
            if not _member(sha, new_known):
                errors.add(
                    w,
                    f"registered hash {sha} disappeared (keep it in previous_manifest_sha256)",
                )
        if old.get("registered_at") != new.get("registered_at"):
            errors.add(w, "registered_at must not change")
        old_status, new_status = old.get("status"), new.get("status")
        if old_status == "retired" and new_status != "retired":
            errors.add(w, "a retired hypothesis cannot be revived; register a new id")
        elif (
            _member(old_status, STATUS_ORDER)
            and _member(new_status, STATUS_ORDER)
            and STATUS_ORDER[new_status] < STATUS_ORDER[old_status]
        ):
            errors.add(
                w, f"status moved backwards from {old_status!r} to {new_status!r}"
            )


# --------------------------------------------------------------- entry
def run(root: Path, base_registry: Path | None = None) -> list[str]:
    errors = Errors()
    registry = _load_json(root / REGISTRY_PATH, str(REGISTRY_PATH), errors)
    by_id = validate_registry(registry, root, errors) if registry is not None else {}
    validate_reports(root, by_id, errors)
    heartbeat = _load_json(root / HEARTBEAT_PATH, str(HEARTBEAT_PATH), errors)
    if heartbeat is not None:
        validate_heartbeat(heartbeat, by_id, errors)
    if base_registry is not None:
        base = _load_json(base_registry, "base-registry", errors)
        if base is not None:
            validate_against_base(base, by_id, errors)
    return errors.items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--base-registry",
        type=Path,
        default=None,
        help="registry.json from the base branch; enforces append-only k and looks",
    )
    parser.add_argument(
        "--print-hash",
        type=Path,
        default=None,
        help="print the canonical sha256 of one manifest file and exit",
    )
    args = parser.parse_args(argv)
    if args.print_hash is not None:
        errors = Errors()
        manifest = _load_json(args.print_hash, str(args.print_hash), errors)
        if manifest is None:
            print("\n".join(errors.items), file=sys.stderr)
            return 1
        print(canonical_sha256(manifest))
        return 0
    try:
        problems = run(args.root, args.base_registry)
    except Exception as exc:  # fail closed with a clean line, never a traceback
        problems = [f"internal error {exc.__class__.__name__}: {exc}"]
    if problems:
        for line in problems:
            print(f"experiment-registry: {line}", file=sys.stderr)
        print(
            f"experiment-registry: FAIL ({len(problems)} problem(s))", file=sys.stderr
        )
        return 1
    print("experiment-registry: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

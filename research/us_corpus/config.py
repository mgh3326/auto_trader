"""us-corpus-v1 build literals.

🔴 SURVIVORSHIP_BIASED = TRUE
The universe is a frozen snapshot of *currently active* US common stocks. Symbols
that delisted before the snapshot are absent, so any return computed from this
corpus is biased upward. Never cite numbers from this corpus without that label.

Every value here is a literal handed down by the job brief. Nothing is inferred
and nothing is read from the operating database.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CORPUS_ID = "us-corpus-v1"
PURPOSE = "EXPLORATORY_BACKTEST_RESEARCH_ONLY"
SURVIVORSHIP_BIASED = True
SOURCE_PRODUCT = "yahoo_finance"
SOURCE_FALLBACK = None  # 🔴 no second source may fill a yahoo gap

ARTIFACT_ROOT = Path("/Users/mgh3326/work/herdr-artifacts/us-corpus-v1")
UNIVERSE_FILE = ARTIFACT_ROOT / "inputs" / "common_stock_universe.csv"
UNIVERSE_FILE_SHA256 = (
    "ac0871793604693381048cd30472059a71a76b4bec6b942502b3b23f9d9ba3b7"
)
UNIVERSE_COUNT = 5355

CROSSCHECK_MODE = "FROZEN_DB_SAMPLE"

# v2 supersedes v1. The v1 export carried a timezone shift: every row's date was
# one calendar day early, which R1 correctly measured and reported as a lag+1
# alignment (634 same-date mismatches). v2 fixes the export; the five value
# columns are byte-identical across the two files for all 1,414 rows, only the
# date labels moved. 🔴 v1 is retained unmodified as correction provenance — it
# is never read by the build, and it is never deleted.
CROSSCHECK_FILE = ARTIFACT_ROOT / "crosscheck" / "kis_db_frozen_sample_v2.csv"
CROSSCHECK_FILE_SHA256 = (
    "a360e988b619029a0641b9a0a596e534a09e734497c92617de79f56c0fae2018"
)
CROSSCHECK_VERSION = "v2"
CROSSCHECK_SUPERSEDED_FILE = ARTIFACT_ROOT / "crosscheck" / "kis_db_frozen_sample.csv"
CROSSCHECK_SUPERSEDED_SHA256 = (
    "bc0f8ca0276f0ad9b570d920312dea44cce3ad2dbb988e0f6bcc352313638d57"
)
CROSSCHECK_SUPERSEDED_MISMATCHES = 634

START_DATE = "2016-01-01"
CUTOFF_SESSION = "2026-07-31"
FREQUENCY = "1d"
PRICE_MODE = "adjusted"
SESSION_CALENDAR = "XNYS"
TIMEZONE = "America/New_York"

TRAIN = ("2016-01-01", "2022-12-31")
VALIDATION = ("2023-01-01", "2024-12-31")
EXPLORATION = ("2016-01-01", "2024-12-31")  # TRAIN + VALIDATION
HOLDOUT_WINDOW = ("2025-01-01", "2026-07-31")
FORWARD_OOS_START = "2026-08-03"

DATASET_DIR = ARTIFACT_ROOT / "dataset"
HOLDOUT_DIR = ARTIFACT_ROOT / "holdout"
HOLDOUT_ACCESS_LOG = ARTIFACT_ROOT / "holdout-access.log"
STAGING_DIR = ARTIFACT_ROOT / "_staging"
REPORTS_DIR = ARTIFACT_ROOT / "reports"
PROBE_DIR = ARTIFACT_ROOT / "probe"
CHECKPOINT_FILE = STAGING_DIR / "checkpoint.jsonl"

PROGRESS_LOG = Path(
    "/Users/mgh3326/work/herdr-inbox/jobs/us-corpus-v1-20260803-1125/events/progress.md"
)

# --- budget rails (🔴 not raisable from inside this process) -----------------
MIN_REQUEST_INTERVAL_SEC = 0.6
MAX_REQUESTS = 12000
MAX_WALL_CLOCK_HOURS = 12
MAX_ARTIFACT_GIB = 10
MAX_RETRIES_PER_SYMBOL = 2

OPERATING_DB_WRITES = 0
TERMINAL_VERDICTS = ("READY_FOR_RESEARCH", "BUILT_WITH_GAPS", "BLOCKED_PRECONDITION")

OHLCV_COLUMNS = ["symbol", "session_date", "open", "high", "low", "close", "volume"]


def sha256_file(path: Path) -> str:
    """Stream a file through SHA-256 without holding it in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def code_commit_sha() -> str:
    """The exact commit the running code came from, stamped into the manifest.

    R1 shipped artifacts generated hours before the final commit, so the
    reported per-symbol breakdown did not reproduce from the shipped HEAD. The
    manifest now records which commit produced it and whether the tree was
    dirty, making that drift visible instead of silent.
    """
    import subprocess

    root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:  # pragma: no cover
        return f"UNKNOWN ({type(exc).__name__})"
    return f"{sha}{'-dirty' if dirty else ''}"


class PreconditionFailed(RuntimeError):
    """Raised when an input file fails its pinned digest — never read past this."""


def verify_inputs() -> dict[str, str]:
    """🔴 Re-verify both pinned digests *before* any read of their contents."""
    verified: dict[str, str] = {}
    for path, expected in (
        (UNIVERSE_FILE, UNIVERSE_FILE_SHA256),
        (CROSSCHECK_FILE, CROSSCHECK_FILE_SHA256),
    ):
        if not path.exists():
            raise PreconditionFailed(f"missing pinned input: {path}")
        actual = sha256_file(path)
        if actual != expected:
            raise PreconditionFailed(
                f"digest mismatch for {path}: expected {expected}, got {actual}"
            )
        verified[path.name] = actual
    return verified

"""Frozen configuration for the kr-corpus-v1 collection job.

The values in this module are the signed job literals.  The collector accepts
no CLI overrides for them: a different corpus requires a separately approved
job and configuration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CorpusConfig:
    corpus_id: str = "kr-corpus-v1"
    purpose: str = "EXPLORATORY_BACKTEST_RESEARCH_ONLY"
    admissible_for_current_krb1: str = "NO"
    source_product: str = "pykrx"
    pykrx_version: str = "1.2.8"
    auto_trader_base_commit: str = "72061c84da01f5dbf4eea3242c9ad5a0cdb4642f"
    auth_mode: str = "DEDICATED_RESEARCH_CREDENTIALS"
    env_file: str = (
        "/Users/mgh3326/services/auto_trader/shared/.env.krx-research.native"
    )
    venv_dir: str = "/Users/mgh3326/services/auto_trader/research-venvs/kr-corpus-v1"
    venv_python: str = "3.13.13"
    code_dir: str = "research/kr_corpus/"
    artifact_root: str = "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/"
    start_date: str = "2015-01-01"
    cutoff_session: str = "2026-07-31"
    markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")
    exclude_markets: tuple[str, ...] = ("KONEX",)
    frequency: str = "1d"
    price_mode: str = "adjusted"
    timezone: str = "Asia/Seoul"
    session_calendar: str = "XKRX"
    train: str = "2015-01-01..2022-12-31"
    validation: str = "2023-01-01..2024-12-31"
    historical_oos: str = "2025-01-01..2026-07-31"
    forward_oos_start: str = "2026-08-03"
    holdout_custody: str = "SEPARATE_PATH_PLUS_ACCESS_LOG"
    holdout_dir: str = "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/holdout/"
    holdout_access_log: str = (
        "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/holdout-access.log"
    )
    max_concurrency: int = 1
    min_request_interval_sec: float = 1.2
    max_requests: int = 15000
    max_wall_clock_hours: int = 12
    max_artifact_gib: int = 20
    min_market_year_membership_bar_coverage: float = 0.995
    crosscheck_mode: str = "FROZEN_KIS_SAMPLE"
    crosscheck_file: str = "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/crosscheck/kis_frozen_sample.csv"
    crosscheck_file_sha256: str = (
        "e648cffb1aeb501939b1ebb89abaec820f3833c52705bdb9651ba045d851f0b4"
    )
    source_fallback: str = "NONE"
    operating_db_writes: int = 0
    db_migrations: int = 0
    broker_or_account_calls: int = 0
    scheduler_changes: int = 0
    deployment: int = 0
    raw_data_redistribution: str = "FORBIDDEN"
    terminal_verdicts: tuple[str, ...] = (
        "READY_FOR_RESEARCH",
        "BUILT_WITH_GAPS",
        "BLOCKED_PRECONDITION",
    )
    # This identifier is fixed to the signed job event path.  It enables a
    # stopped process to resume its own .partial snapshot without discovering
    # or replacing any other corpus snapshot.
    run_id: str = "kr-corpus-v1-20260803-1001"
    progress_path: str = (
        "/Users/mgh3326/work/herdr-inbox/jobs/kr-corpus-v1-20260803-1001/"
        "events/progress.md"
    )

    @property
    def artifact_root_path(self) -> Path:
        return Path(self.artifact_root)

    @property
    def holdout_root_path(self) -> Path:
        return Path(self.holdout_dir)

    @property
    def progress_file_path(self) -> Path:
        return Path(self.progress_path)

    @property
    def source_start(self) -> str:
        return self.start_date.replace("-", "")

    @property
    def source_cutoff(self) -> str:
        return self.cutoff_session.replace("-", "")

    @property
    def historical_oos_start(self) -> str:
        return self.historical_oos.split("..", maxsplit=1)[0]

    @property
    def historical_oos_end(self) -> str:
        return self.historical_oos.split("..", maxsplit=1)[1]

    def is_holdout_session(self, session: str) -> bool:
        return self.historical_oos_start <= session <= self.historical_oos_end

    def public_dict(self) -> dict[str, object]:
        """Return all signed literals without credential values."""
        return asdict(self)


FROZEN_CONFIG = CorpusConfig()

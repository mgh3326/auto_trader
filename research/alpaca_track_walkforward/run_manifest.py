"""Immutable run identity for ROB-1062 H4.

H2 deliberately carries no fabricated H1 dataset-manifest hash: the real,
operator-approved H1 archive corpus has not been collected.  H4 therefore
has exactly one runnable identity today, the committed synthetic AC27
fixture.  This module pins that fixture's anchor and every input-artifact
digest used by each fold/family.  A future real run must add an
operator-approved manifest whose H1 ``CorpusManifest.content_hash()`` is
fixed here; callers cannot nominate an authority at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import canonical_hash
from daily_bars import DailyBar
from pit_universe_alpaca import UniverseSnapshot

__all__ = [
    "CanonicalRunManifest",
    "RunManifestError",
    "canonical_daily_bars_hash",
    "canonical_minute_grid_hash",
    "canonical_run_manifest",
    "canonical_universe_grid_hash",
]

DAY_MS = 86_400_000
CANONICAL_ANCHOR_OOS_START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
CANONICAL_RUN_ID = "rob1062-h4-synthetic-ac27-v1"
CANONICAL_SOURCE_KIND = "synthetic_fixture"
CANONICAL_SYMBOLS = tuple(f"SYM{index:02d}/USD" for index in range(20))


class RunManifestError(ValueError):
    """A runtime input differs from the immutable run authority."""


def _bar_payload(bar: DailyBar) -> list:
    if type(bar) is not DailyBar:
        raise TypeError("daily corpus must contain exact DailyBar values")
    return [
        bar.day_start_ms,
        bar.day_end_ms,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.minute_count_observed,
        bar.imputed_minutes,
        bar.max_gap_minutes,
        bar.gap_in_last_60min,
        bar.is_valid,
        bar.is_segment_start,
    ]


def canonical_daily_bars_hash(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
) -> str:
    return canonical_hash.canonical_sha256(
        {
            "bars_by_symbol": {
                symbol: [_bar_payload(bar) for bar in bars_by_symbol[symbol]]
                for symbol in sorted(bars_by_symbol)
            }
        }
    )


def _universe_payload(snapshot: UniverseSnapshot) -> dict:
    if type(snapshot) is not UniverseSnapshot:
        raise TypeError("universe grid must contain exact UniverseSnapshot values")
    return {
        "decision_ts_ms": snapshot.decision_ts_ms,
        "eligible_symbols": list(snapshot.eligible_symbols),
        "per_symbol": [item.to_dict() for item in snapshot.per_symbol],
        "n_t": snapshot.n_t,
        "meets_min_universe_size": snapshot.meets_min_universe_size,
    }


def canonical_universe_grid_hash(
    snapshots_by_ts: Mapping[int, UniverseSnapshot],
) -> str:
    return canonical_hash.canonical_sha256(
        {
            "universe_by_ts": [
                [timestamp, _universe_payload(snapshots_by_ts[timestamp])]
                for timestamp in sorted(snapshots_by_ts)
            ]
        }
    )


def canonical_minute_grid_hash(
    bars_by_key: Mapping[tuple[str, int], Sequence],
) -> str:
    payload: list[list] = []
    for symbol, signal_ts_ms in sorted(bars_by_key):
        bars = bars_by_key[(symbol, signal_ts_ms)]
        payload.append(
            [
                symbol,
                signal_ts_ms,
                [
                    [
                        bar.open_time_ms,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                    ]
                    for bar in bars
                ],
            ]
        )
    return canonical_hash.canonical_sha256({"minute_bars_by_key": payload})


_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class CanonicalRunManifest:
    run_id: str
    source_kind: str
    anchor_oos_start_ms: int
    symbols: tuple[str, ...]
    daily_bars_hash_by_fold: Mapping[str, str]
    universe_grid_hash_by_fold_family: Mapping[str, str]
    minute_grid_hash_by_fold_family: Mapping[str, str]
    manifest_hash: str
    _construction_token: object

    def __post_init__(self) -> None:
        if self._construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("CanonicalRunManifest is issued only by this module")
        object.__setattr__(self, "symbols", tuple(self.symbols))
        for mapping_name in (
            "daily_bars_hash_by_fold",
            "universe_grid_hash_by_fold_family",
            "minute_grid_hash_by_fold_family",
        ):
            mapping = dict(getattr(self, mapping_name))
            object.__setattr__(
                self,
                mapping_name,
                MappingProxyType(dict(sorted(mapping.items()))),
            )

    def assert_daily_bars(self, *, fold_id: str, actual_hash: str) -> None:
        expected = self.daily_bars_hash_by_fold.get(fold_id)
        if expected is None or actual_hash != expected:
            raise RunManifestError(
                f"daily corpus hash does not match canonical {fold_id} artifact"
            )

    def assert_provider_grids(
        self,
        *,
        fold_id: str,
        family: str,
        universe_grid_hash: str,
        minute_grid_hash: str,
    ) -> None:
        self.assert_universe_grid(
            fold_id=fold_id,
            family=family,
            actual_hash=universe_grid_hash,
        )
        self.assert_minute_grid(
            fold_id=fold_id,
            family=family,
            actual_hash=minute_grid_hash,
        )

    def assert_universe_grid(
        self, *, fold_id: str, family: str, actual_hash: str
    ) -> None:
        key = f"{fold_id}:{family}"
        if self.universe_grid_hash_by_fold_family.get(key) != actual_hash:
            raise RunManifestError(
                f"universe grid hash does not match canonical {key} artifact"
            )

    def assert_minute_grid(
        self, *, fold_id: str, family: str, actual_hash: str
    ) -> None:
        key = f"{fold_id}:{family}"
        if self.minute_grid_hash_by_fold_family.get(key) != actual_hash:
            raise RunManifestError(
                f"minute grid hash does not match canonical {key} artifact"
            )


# Generated from the deterministic synthetic fixture and reviewed as part of
# the implementation commit. These are source-input identities, not strategy
# thresholds.
_DAILY_HASHES = {
    "fold-0": "553a05325c38edb4f2744956c10a8ab398df431d6d9ff91d3f3046690b261f96",
    "fold-1": "756c051d0b8a85b4c4b10f5255e5bf537496313405eb9d14520d39158f620bd2",
    "fold-2": "eef6a24886063bbbb582c71ec222e645f559c45122655491fabe615c8c8582e1",
    "fold-3": "5349e5ae3a9e8951115c86e5fff195abd2d03479a79b34c4ffeba3c71a99256c",
    "fold-4": "b788911bf968ee95208b404e434d75922436fa960f2255f5293898620b2a9984",
    "fold-5": "8ba808a848b578be0309335909c0e63df343139c0af993fcde85af2f35b6b150",
    "fold-6": "d37ff902e9ac8a329814211904ef6dbdf7a6044b4fa49bf8661e98bf0f0fae0f",
    "fold-7": "b31253c8bfbc63aadf2b9ae3d7a82edb4d332e062e07574de66fed4a4b48a8ee",
}
_UNIVERSE_HASHES = {
    "fold-0:AP-A1": "3811ac111ceb1dd796710003f206881a5ffd1b80b9c4832622e2b3cdcc41fa11",
    "fold-0:AP-A2": "7db0083ab034e459f045052c8bad42be77a2ccf5df0c7828de4f1eb6557b22f7",
    "fold-1:AP-A1": "2192bb777b84d7b9ae793771089e79bc900e2ed38411e6606fc5378b6de73eaf",
    "fold-1:AP-A2": "3ef1f5e4cbee65b1c041d23b14391123683acdcd4a72831a91da8aa02fc5df00",
    "fold-2:AP-A1": "56ab91764161c36ef734d3d9179ae6e7dbff3cc2fe00d50bdb47d208e3105248",
    "fold-2:AP-A2": "6a7e635f68ce3a7d9fbeae9332924b2c9908e52174071c2f73693bdb0d31d684",
    "fold-3:AP-A1": "733b360f89821e45d61ceab0a279e5318fc3db74918a15d037d5b740870f1941",
    "fold-3:AP-A2": "e8618c35df6b017cd84dca47a4607baa9986090ca90b6c9924138970a35f8e40",
    "fold-4:AP-A1": "45a3145da0494c12135a3c559060d1febbc2446f95f8761fe7e58d9f1b1305be",
    "fold-4:AP-A2": "d2d028c9803092498d5fa7850591f6d945bf9611a624b3665e68be6bc000964a",
    "fold-5:AP-A1": "3114608742ef9ff19551ab3d4668922f699a9398f45c56366239bfc38494e1df",
    "fold-5:AP-A2": "dee3b48ced1516013481dad9aaa7f3a0ea5c6f6fa0b6d11407c06ddb19a72e67",
    "fold-6:AP-A1": "cf5c5a7d4817695b4f9bc2555269b8a1809dd76c4ddefcc956ed7662fc7b82d8",
    "fold-6:AP-A2": "1064e20936edc6c638ebbe5364482b0073f150e5892e28265ed2a5a4d57af627",
    "fold-7:AP-A1": "c331085948ab44667e615619770b1720a96700adf48f15ed27c7ea4562b47d29",
    "fold-7:AP-A2": "ebb3994ad7022776f0a736df0fa8e201e82002f8070dccd1008adbcf2644dc59",
}
_MINUTE_HASHES = {
    "fold-0:AP-A1": "b74732a30da4de5fe4970c45555911c1019d8cc6bdfe2276eb3bcac5bbb1b5d8",
    "fold-0:AP-A2": "a687e88607bdfb2247110e4a0f244d7e17963b383290072afa78006c8b31bc96",
    "fold-1:AP-A1": "2e292e4b50cc7308a4ac333e86a3cc208c5c4e8a09268188a7a5f5660c6d10fd",
    "fold-1:AP-A2": "11dac9610dae09292fda1f2c6706dfb2ec3ce5770a2429ec58eeb21f2817b1df",
    "fold-2:AP-A1": "76c758fd69287e6a2c324001f71a01c04c3499ace8fffbafa4a09c58934a3484",
    "fold-2:AP-A2": "d3e579f270829959b34ac749a721718632676a489232e754a1ea0a7b69c2cc05",
    "fold-3:AP-A1": "cd722aaab16add4a3e6d4597434dd7ff0de32a64ededd851b3d4dbc096681058",
    "fold-3:AP-A2": "b3b0c406dccf2bc9590ab70e5e7d9f05caa93a8126bd7e21483bc74fcb25bc67",
    "fold-4:AP-A1": "1daf8e6fe553620084926d139b60f0d79f04a2f78763e832c4283b513965cc72",
    "fold-4:AP-A2": "9c8662eeea4636aba239410184b5844053b6932fa957d08aa252358f9efc2881",
    "fold-5:AP-A1": "84691f4678e58f15bff55e8869c97bbe37b90406a9ae2f82e443484dae4d3143",
    "fold-5:AP-A2": "b594b7f2b0fe6586b76038b8a82fa4b055af9e71d775cfcb48f1ec2223f4487e",
    "fold-6:AP-A1": "52a6c1be58ad63bc0080a6c63cab71a9932fa709d429deb4b455167e09bcb6a7",
    "fold-6:AP-A2": "7b0a2651603279997cd3056bab6d8653551ba936b5c0e5281c88d407ff9f458a",
    "fold-7:AP-A1": "dba362f3e2490017bd037d5ceb14d7874a6d38ef14eb36b1a9b7d0b8d0750399",
    "fold-7:AP-A2": "bc1a46a526c8c8d82f2105b612ae942143b87c1ce8521d7287e247a96dd165f2",
}


def _manifest_payload() -> dict:
    return {
        "run_id": CANONICAL_RUN_ID,
        "source_kind": CANONICAL_SOURCE_KIND,
        "anchor_oos_start_ms": CANONICAL_ANCHOR_OOS_START_MS,
        "symbols": list(CANONICAL_SYMBOLS),
        "daily_bars_hash_by_fold": _DAILY_HASHES,
        "universe_grid_hash_by_fold_family": _UNIVERSE_HASHES,
        "minute_grid_hash_by_fold_family": _MINUTE_HASHES,
    }


_CANONICAL_MANIFEST = CanonicalRunManifest(
    **_manifest_payload(),
    manifest_hash=canonical_hash.canonical_sha256(_manifest_payload()),
    _construction_token=_CONSTRUCTION_TOKEN,
)


def canonical_run_manifest() -> CanonicalRunManifest:
    """Return the sole code-pinned run authority; no caller-selected path."""
    return _CANONICAL_MANIFEST

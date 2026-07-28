"""Immutable run identity for ROB-1062 H4.

H2 deliberately carries no fabricated H1 dataset-manifest hash: the real,
operator-approved H1 archive corpus has not been collected.  H4 therefore
has exactly one runnable identity today, the committed synthetic AC27
fixture.  This module pins that fixture's anchor and every input-artifact
digest used by each fold/family.  A future real run must add an
operator-approved manifest whose H1 ``CorpusManifest.content_hash()`` is
fixed here; callers cannot nominate an authority at runtime.

Identity ``...-v2`` supersedes ``...-v1``.  v1's corpus generator keyed
prices off each fold's own window start, so all eight walk-forward folds
observed the same price path and the terminal artifact held 16 distinct
observations replicated eight times (see ``synthetic_corpus`` module
docstring).  v2 derives every price from absolute UTC calendar time, so the
eight folds are eight different periods of one history.  Every daily and
minute digest below therefore changed; the universe digests did not, because
eligibility never depended on price.  v1's artifact and the H6 seals built
from it stay on disk as historical evidence and are NOT loadable under this
identity — that rejection is the intended fail-closed behaviour.
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
CANONICAL_RUN_ID = "rob1062-h4-synthetic-ac27-v2"
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
# thresholds. Regenerated for identity v2 (absolute-time corpus): all eight
# daily digests and all sixteen minute digests moved because every fold now
# observes a different segment of one history; the universe digests are
# byte-identical to v1 because eligibility is price-independent.
_DAILY_HASHES = {
    "fold-0": "59d6a40a63ccf31b19a1273b6ccfe68044fc52c40f470abf53d7f6d592a2a974",
    "fold-1": "a488ffbbc9f3c0af604c93d34c1df20c8bffebe4de558a1e6463510ee598c402",
    "fold-2": "03be33cb21794db0d9b3d7e9cef300ffa47c6176f1d67549379a3a47a2a6374f",
    "fold-3": "d9dcbf0d0634df692641f8298b48e716ebb94c5851582e5c86e1ed9374a9ad95",
    "fold-4": "60ccd7854f3a8116c4c1b5f3c5c85269f7dfc8ec0137663658ce533872fc1142",
    "fold-5": "f12420f4075a6f4d734ba959644bfe12a82a3258eb55774c54156f5804d4f4b7",
    "fold-6": "e51d3f1d08f722e7eaeae550f622849be18104e53d25df153806dc20435eb660",
    "fold-7": "764fb8d731321b506d696176c6960064e1263b6c33a42adcaf698342b67657e9",
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
    "fold-0:AP-A1": "43ddda4b2cde16333974fad95b5659ca413354413633511b5b5ed4a41d372ad3",
    "fold-0:AP-A2": "3521f4461128046193a4f49ec69c0f02ed306de623c2473ace450ccf445d2aef",
    "fold-1:AP-A1": "c20c24a1be981b08f665401281158bfe486fbccfa9c6f4cbdba8d72e814e88d7",
    "fold-1:AP-A2": "f09102cc5632f24af2be5b39f98fa0f4d466fe408fb1ab85ed2561266049e055",
    "fold-2:AP-A1": "f657a1d38838e9b93da7d617ab19551fb5097b70c3056a5d87f524c2379b24b4",
    "fold-2:AP-A2": "19d7a6aa87a92ed712e757385041ed5427349e00a63542c5e6a4f1832f8afb71",
    "fold-3:AP-A1": "a55a96a4783e13018f9eaa629962415f2c5f73c1339e90616d9afa21fa35ca08",
    "fold-3:AP-A2": "e9ad33be36ff7cce8fc58e71b843717e0b0d09e27aaeba7db671df77b0e86b2d",
    "fold-4:AP-A1": "755e76788e7add3ddc996642524d134a45c73ddb92c4fcd386e9c63452ef1db3",
    "fold-4:AP-A2": "b9bf6c922258ffa4cee21b70f018a50ab4b79af1d93b2dd5f62b016450add335",
    "fold-5:AP-A1": "e383bf3211b63a5fbf97cfb1f55df63fc204befaa14d5c2e41f0d1324cdff6e0",
    "fold-5:AP-A2": "5335dc630c303f0097f81cfd8227ca53027e6a91109a8db50eef64f3b941d503",
    "fold-6:AP-A1": "a11129503f477c27ae8030be9d22cecc9cdd1c5b7aca70294afef6f1afa3c8d7",
    "fold-6:AP-A2": "1acac39d81e989e9f29f40c4a99085760fd171842a97b33b3fb4de16ac55f710",
    "fold-7:AP-A1": "b531db22fe1e5525a69bdcadaca32e5037d961b6a53688b1777901e73be9d827",
    "fold-7:AP-A2": "a608a669b5e01eced7c6f1f825b37ed154a06446cc2b88ea47bf8d5d5bb382ff",
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

from __future__ import annotations

import hashlib
import json

import pytest

from scripts import replay_cr_s1_etr as replay


def test_replay_driver_is_bounded_to_the_two_frozen_etr_pairs() -> None:
    assert set(replay._FROZEN_PAIR_SPECS) == {  # noqa: SLF001 - frozen CLI contract
        "upbit_krw",
        "binance_usdt_spot",
    }
    assert replay._FROZEN_PAIR_SPECS["upbit_krw"].output_filename == (  # noqa: SLF001
        "cr-spot-etr-01__upbit_krw.json"
    )
    assert replay._FROZEN_PAIR_SPECS["binance_usdt_spot"].output_filename == (  # noqa: SLF001
        "cr-spot-etr-01__binance_usdt_spot.json"
    )


def test_replay_record_writer_is_atomic_and_refuses_overwrite(tmp_path) -> None:
    target = tmp_path / "cr-spot-etr-01__upbit_krw.json"
    payload = {"schema_version": "test", "value": [1, 2, 3]}

    digest = replay.write_json_once(target, payload)

    expected_bytes = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    assert digest == hashlib.sha256(expected_bytes).hexdigest()
    assert target.read_bytes() == expected_bytes
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        replay.write_json_once(target, {"schema_version": "replacement"})
    assert target.read_bytes() == expected_bytes


def test_replay_driver_and_engine_manifest_are_content_bound() -> None:
    driver_sha256 = replay._sha256_file(replay._DRIVER_PATH)  # noqa: SLF001
    manifest = replay._code_blob_manifest()  # noqa: SLF001

    assert len(driver_sha256) == 64
    assert all(len(digest) == 64 for digest in manifest.values())
    assert replay._code_blob_manifest_sha256(manifest) == replay._sha256_json(manifest)  # noqa: SLF001

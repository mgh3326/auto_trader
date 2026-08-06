from __future__ import annotations

from scripts import resynthesize_cr_s1_b2_correction as driver


def test_resynthesis_driver_is_bounded_to_corrected_four_plus_etr_two() -> None:
    assert set(driver._FROZEN_PAIR_SOURCES) == {  # noqa: SLF001 - frozen CLI scope
        "cr-spot-tpr-01__upbit_krw.json",
        "cr-spot-tpr-01__binance_usdt_spot.json",
        "cr-spot-ceb-01__upbit_krw.json",
        "cr-spot-ceb-01__binance_usdt_spot.json",
        "cr-spot-etr-01__upbit_krw.json",
        "cr-spot-etr-01__binance_usdt_spot.json",
    }
    assert set(driver._FROZEN_PAIR_SOURCES) == set(  # noqa: SLF001
        driver._EXPECTED_PAIR_SHA256  # noqa: SLF001
    )
    assert set(driver._FROZEN_MANIFEST_SOURCES) == {  # noqa: SLF001
        "tpr_ceb_correction_manifest.json",
        "etr_b1_manifest.json",
    }
    assert set(driver._FROZEN_MANIFEST_SOURCES) == set(  # noqa: SLF001
        driver._EXPECTED_MANIFEST_SHA256  # noqa: SLF001
    )


def test_resynthesis_driver_refuses_a_source_output_directory() -> None:
    source_directory = next(
        iter(driver._FROZEN_PAIR_SOURCES.values())  # noqa: SLF001
    ).parent

    try:
        driver._refuse_source_output_directory(source_directory)  # noqa: SLF001
    except ValueError as exc:
        assert "outside every sealed source directory" in str(exc)
    else:  # pragma: no cover - explicit failure reads more clearly than pytest.raises
        raise AssertionError(
            "source directory must be refused as an output destination"
        )

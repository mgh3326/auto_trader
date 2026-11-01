"""The health check fails on a missing day, a stale dataset and a corrupt
partition, and pings the URL from the named env var without printing it."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from research.data_plane import cli, health
from research.data_plane.calendars import bar_available_at
from research.data_plane.contract import DATASET_BARS, iso, make_row
from research.data_plane.health import check_health, ping_url
from research.data_plane.store import dataset_lock, partition_path, publish_partition

# KR sessions Mon 2026-10-26 .. Fri 2026-10-30, then Mon 11-02, Tue 11-03.
WEEK = [date(2026, 10, d) for d in (26, 27, 28, 29, 30)]


def _seed(root, days, *, coverage=1.0):
    for day in days:
        available = bar_available_at("kr", day)
        row = make_row(
            instrument="005930",
            venue="KOSPI",
            event_time=iso(available),
            available_at=iso(available),
            observed_at=iso(available),
            source="fixture",
            source_version="v1",
            bar_date=day.isoformat(),
            open="1",
            high="1",
            low="1",
            close="1",
            volume=1,
        )
        with dataset_lock(root, DATASET_BARS, "kr"):
            publish_partition(
                root,
                dataset=DATASET_BARS,
                market="kr",
                day=day,
                rows=[row],
                meta={"coverage": coverage},
            )


def _kinds(report):
    return sorted({(p["kind"], p.get("partition")) for p in report.problems})


FRI_NIGHT = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)  # 22:00 KST Friday


@pytest.mark.unit
def test_complete_week_is_healthy(tmp_path):
    _seed(tmp_path, WEEK)
    report = check_health(tmp_path, market="kr", dataset=DATASET_BARS, now=FRI_NIGHT)
    assert report.healthy, report.problems
    assert report.due == [d.isoformat() for d in WEEK]


@pytest.mark.unit
def test_missing_day_fails_as_gap(tmp_path):
    _seed(tmp_path, [d for d in WEEK if d != date(2026, 10, 28)])
    report = check_health(tmp_path, market="kr", dataset=DATASET_BARS, now=FRI_NIGHT)
    assert not report.healthy
    assert _kinds(report) == [("gap", "2026-10-28")]


@pytest.mark.unit
def test_stale_dataset_fails(tmp_path):
    _seed(tmp_path, WEEK)
    tue_night = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)  # 22:00 KST Tue 11-03
    report = check_health(tmp_path, market="kr", dataset=DATASET_BARS, now=tue_night)
    assert not report.healthy
    assert _kinds(report) == [("stale", "2026-11-02"), ("stale", "2026-11-03")]


@pytest.mark.unit
def test_day_not_yet_due_is_not_stale(tmp_path):
    _seed(tmp_path, WEEK[:-1])
    fri_afternoon = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)  # 15:00 KST, before close
    report = check_health(
        tmp_path, market="kr", dataset=DATASET_BARS, now=fri_afternoon
    )
    assert report.healthy, report.problems


@pytest.mark.unit
def test_weekend_and_holiday_are_not_gaps(tmp_path):
    _seed(tmp_path, WEEK)
    sun = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert check_health(tmp_path, market="kr", dataset=DATASET_BARS, now=sun).healthy


@pytest.mark.unit
def test_corrupt_and_low_coverage_partitions_fail(tmp_path):
    _seed(tmp_path, WEEK[:2])
    _seed(tmp_path, WEEK[2:], coverage=0.5)
    path = partition_path(tmp_path, DATASET_BARS, "kr", WEEK[0])
    path.unlink()
    path.write_bytes(b"tampered")
    report = check_health(tmp_path, market="kr", dataset=DATASET_BARS, now=FRI_NIGHT)
    kinds = _kinds(report)
    assert ("corrupt", "2026-10-26") in kinds
    assert ("low_coverage", "2026-10-28") in kinds


@pytest.mark.unit
def test_empty_dataset_fails(tmp_path):
    report = check_health(tmp_path, market="kr", dataset=DATASET_BARS, now=FRI_NIGHT)
    assert _kinds(report) == [("empty", None)]


@pytest.mark.unit
def test_cli_exit_code_and_fail_ping_without_leaking_url(tmp_path, monkeypatch, capsys):
    _seed(tmp_path, [d for d in WEEK if d != date(2026, 10, 28)])
    secret_url = "https://hc.example.test/ping/abc-secret-uuid"
    monkeypatch.setenv("DP_TEST_PING", secret_url)
    pinged: list[str] = []
    monkeypatch.setattr(health, "_default_ping", pinged.append)

    code = cli.main(
        [
            "health",
            "--root",
            str(tmp_path),
            "--market",
            "kr",
            "--dataset",
            "bars_1d",
            "--ping-url-env",
            "DP_TEST_PING",
            "--now",
            FRI_NIGHT.isoformat(),
        ]
    )

    out = capsys.readouterr()
    assert code == 1
    assert pinged == [secret_url + "/fail"]
    assert "abc-secret-uuid" not in out.out + out.err
    assert json.loads(out.out)["ping"] == "sent_fail"


@pytest.mark.unit
def test_ping_states(monkeypatch):
    sent: list[str] = []
    assert ping_url(None, True, ping=sent.append) == "not_configured"
    monkeypatch.setenv("DP_PING_HTTP", "http://insecure.example.test/x")
    assert ping_url("DP_PING_HTTP", True, ping=sent.append) == "refused_non_https"
    monkeypatch.setenv("DP_PING_OK", "https://hc.example.test/x/")
    assert ping_url("DP_PING_OK", True, ping=sent.append) == "sent_ok"

    def boom(_url: str) -> None:
        raise OSError("down")

    assert ping_url("DP_PING_OK", False, ping=boom) == "failed:OSError"
    assert sent == ["https://hc.example.test/x/"]


@pytest.mark.unit
def test_subset_and_repaired_partitions_are_not_healthy(tmp_path):
    from research.data_plane.store import manifest_path

    _seed(tmp_path, WEEK)
    manifest = manifest_path(tmp_path, DATASET_BARS, "kr")
    lines = manifest.read_text().splitlines()
    rec = json.loads(lines[-1])
    rec_subset = {**json.loads(lines[-2]), "instrument_selection": "explicit_subset"}
    rec_repaired = {k: v for k, v in rec.items() if k != "coverage"} | {
        "repaired": True
    }
    manifest.write_text(
        "\n".join([*lines[:-2], json.dumps(rec_subset), json.dumps(rec_repaired)])
        + "\n"
    )
    report = check_health(tmp_path, market="kr", dataset=DATASET_BARS, now=FRI_NIGHT)
    assert _kinds(report) == [
        ("repaired_unverified", "2026-10-30"),
        ("subset_partition", "2026-10-29"),
    ]


@pytest.mark.unit
def test_lookback_must_be_positive():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "health",
                "--root",
                "/x",
                "--market",
                "kr",
                "--dataset",
                "bars_1d",
                "--lookback-days",
                "-1",
            ]
        )

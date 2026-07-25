"""ROB-984 CP2 directory-atomic artifact primitive coverage.

All scorecard meaning in this file is visibly ``contract_fixture`` test data;
the production primitive only calls the supplied H5 port.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest


def _artifacts():
    spec = importlib.util.find_spec("rob974_h6b_artifacts")
    assert spec is not None, "ROB-984 CP2 artifact behavior is not implemented"
    return importlib.import_module("rob974_h6b_artifacts")


def test_cp2_artifact_behavior_is_implemented() -> None:
    assert _artifacts().stage_scorecard_pair


class ContractFixtureH5Port:
    """Issue-derived test-only bytes/hash/renderer seam, never production H5."""

    provenance = "contract_fixture"

    def __init__(self) -> None:
        self.canonical_inputs: list[object] = []
        self.semantic_inputs: list[object] = []
        self.render_inputs: list[object] = []

    def canonical_json_bytes(self, scorecard):
        self.canonical_inputs.append(scorecard)
        return (
            json.dumps(
                scorecard,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode()

    def semantic_hash(self, scorecard):
        self.semantic_inputs.append(scorecard)
        return hashlib.sha256(self.canonical_json_bytes(scorecard)).hexdigest()

    def render_markdown(self, scorecard):
        self.render_inputs.append(scorecard)
        return (
            f"# Contract Fixture Scorecard\n\n"
            f"title: {scorecard['title']}\n"
            f"value: {scorecard['value']}\n"
        ).encode()


def _scorecard():
    return {
        "schema_version": "rob974-h5-contract-fixture.v1",
        "title": "fixture-눈",
        "value": 17,
        "mapping": {"S4": 24, "S3": 24},
    }


def _stage(tmp_path: Path, *, port=None, scorecard=None):
    module = _artifacts()
    port = port or ContractFixtureH5Port()
    scorecard = scorecard or _scorecard()
    output = tmp_path / "scorecard-pair"
    staged = module.stage_scorecard_pair(
        scorecard=scorecard, output_dir=output, h5_port=port
    )
    return module, port, scorecard, output, staged


def test_stage_is_fresh_sibling_exact_pair_fsynced_and_physically_verified(
    tmp_path, monkeypatch
):
    module = _artifacts()
    writes: list[str] = []
    directory_fsyncs: list[Path] = []
    real_write = module._write_exclusive_fsynced
    real_fsync_directory = module._fsync_directory

    def recording_write(path, payload):
        writes.append(path.name)
        return real_write(path, payload)

    def recording_dir_fsync(path):
        directory_fsyncs.append(path)
        return real_fsync_directory(path)

    monkeypatch.setattr(module, "_write_exclusive_fsynced", recording_write)
    monkeypatch.setattr(module, "_fsync_directory", recording_dir_fsync)
    module, port, scorecard, output, staged = _stage(
        tmp_path, port=ContractFixtureH5Port()
    )
    assert not output.exists()
    assert staged.staging_dir.parent == output.parent
    assert staged.staging_dir.name.startswith(f".{output.name}.staging-")
    assert sorted(path.name for path in staged.staging_dir.iterdir()) == [
        "scorecard.json",
        "scorecard.md",
    ]
    assert writes == ["scorecard.json", "scorecard.md"]
    assert directory_fsyncs == [staged.staging_dir]
    assert staged.stage_state == "STAGED_FSYNCED_PHYSICALLY_VERIFIED"
    assert (staged.staging_dir / "scorecard.json").read_bytes() == (
        staged.canonical_json_bytes
    )
    assert (staged.staging_dir / "scorecard.md").read_bytes() == staged.markdown_bytes
    assert port.render_inputs[0] is scorecard
    assert any(item is not scorecard for item in port.render_inputs[1:])


def test_publish_is_one_noreplace_directory_rename_then_parent_fsync(
    tmp_path, monkeypatch
):
    module, port, _score, output, staged = _stage(tmp_path)
    renames: list[tuple[Path, Path]] = []
    parent_fsyncs: list[Path] = []
    real_rename = module._rename_noreplace
    real_fsync = module._fsync_directory

    def recording_rename(source, destination):
        renames.append((source, destination))
        return real_rename(source, destination)

    def recording_fsync(path):
        parent_fsyncs.append(path)
        return real_fsync(path)

    monkeypatch.setattr(module, "_rename_noreplace", recording_rename)
    monkeypatch.setattr(module, "_fsync_directory", recording_fsync)
    published = module.publish_staged_pair(staged, h5_port=port)
    assert renames == [(staged.staging_dir, output)]
    assert parent_fsyncs == [output.parent]
    assert output.exists()
    assert not staged.staging_dir.exists()
    assert published.stage_state == "PUBLISHED_PARENT_FSYNCED"
    assert sorted(path.name for path in output.iterdir()) == [
        "scorecard.json",
        "scorecard.md",
    ]


@pytest.mark.parametrize("filename", ("scorecard.json", "scorecard.md"))
def test_corruption_after_file_fsync_fails_closed_and_preserves_staging(
    tmp_path, monkeypatch, filename
):
    module = _artifacts()
    real_write = module._write_exclusive_fsynced

    def corrupt_after_fsync(path, payload):
        real_write(path, payload)
        if path.name == filename:
            path.write_bytes(payload + b"corrupt")

    monkeypatch.setattr(module, "_write_exclusive_fsynced", corrupt_after_fsync)
    output = tmp_path / "scorecard-pair"
    with pytest.raises(module.ArtifactVerificationError) as raised:
        module.stage_scorecard_pair(
            scorecard=_scorecard(),
            output_dir=output,
            h5_port=ContractFixtureH5Port(),
        )
    assert raised.value.state.stage == "PHYSICAL_READBACK_FAILED"
    assert raised.value.state.staging_dir is not None
    assert raised.value.state.staging_dir.exists()
    assert not output.exists()


def test_renderer_is_called_on_physically_parsed_json_not_only_memory(tmp_path):
    module = _artifacts()
    scorecard = _scorecard()

    class ParsedObjectPoisonPort(ContractFixtureH5Port):
        def render_markdown(self, value):
            rendered = super().render_markdown(value)
            if value is not scorecard:
                return rendered.replace(b"value: 17", b"value: parsed-mutant")
            return rendered

    with pytest.raises(module.ArtifactVerificationError, match="renderer parity"):
        module.stage_scorecard_pair(
            scorecard=scorecard,
            output_dir=tmp_path / "scorecard-pair",
            h5_port=ParsedObjectPoisonPort(),
        )


@pytest.mark.parametrize(
    "bad_json",
    (
        b'{"x":NaN}\n',
        b'{"x":1}',
        b'{"x":1}\r\n',
        b'{"x":1,"x":2}\n',
        b"\xff\n",
        b"[]\n",
    ),
)
def test_nan_newline_duplicate_shape_locale_and_utf8_drift_fail_before_staging(
    tmp_path, bad_json
):
    module = _artifacts()

    class BadCanonicalPort(ContractFixtureH5Port):
        def canonical_json_bytes(self, _scorecard):
            return bad_json

    output = tmp_path / "scorecard-pair"
    with pytest.raises(module.ArtifactError):
        module.stage_scorecard_pair(
            scorecard=_scorecard(), output_dir=output, h5_port=BadCanonicalPort()
        )
    assert not output.exists()
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("bad_markdown", (b"# no-lf", b"# crlf\r\n", b"\xff\n"))
def test_markdown_newline_and_encoding_drift_fail_before_staging(
    tmp_path, bad_markdown
):
    module = _artifacts()

    class BadMarkdownPort(ContractFixtureH5Port):
        def render_markdown(self, _scorecard):
            return bad_markdown

    with pytest.raises(module.ArtifactPreflightError):
        module.stage_scorecard_pair(
            scorecard=_scorecard(),
            output_dir=tmp_path / "scorecard-pair",
            h5_port=BadMarkdownPort(),
        )
    assert tuple(tmp_path.iterdir()) == ()


def test_mapping_order_recanonicalization_drift_fails_physical_readback(tmp_path):
    module = _artifacts()

    class OrderDriftPort(ContractFixtureH5Port):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def canonical_json_bytes(self, scorecard):
            self.calls += 1
            if self.calls == 1:
                return (
                    json.dumps(scorecard, ensure_ascii=False, sort_keys=False) + "\n"
                ).encode()
            return super().canonical_json_bytes(scorecard)

        def semantic_hash(self, _scorecard):
            return "a" * 64

    with pytest.raises(module.ArtifactVerificationError, match="recanonicalize"):
        module.stage_scorecard_pair(
            scorecard=_scorecard(),
            output_dir=tmp_path / "scorecard-pair",
            h5_port=OrderDriftPort(),
        )


def test_preexisting_output_stale_staging_and_path_traversal_are_never_repaired(
    tmp_path,
):
    module = _artifacts()
    output = tmp_path / "scorecard-pair"
    output.mkdir()
    marker = output / "operator-owned"
    marker.write_text("keep")
    with pytest.raises(module.ArtifactCollisionError):
        module.stage_scorecard_pair(
            scorecard=_scorecard(), output_dir=output, h5_port=ContractFixtureH5Port()
        )
    assert marker.read_text() == "keep"

    output.rmdir() if not any(output.iterdir()) else None
    marker.unlink()
    output.rmdir()
    stale = tmp_path / f".{output.name}.staging-forensic"
    stale.mkdir()
    with pytest.raises(module.ArtifactCollisionError):
        module.stage_scorecard_pair(
            scorecard=_scorecard(), output_dir=output, h5_port=ContractFixtureH5Port()
        )
    assert stale.exists()

    traversal = tmp_path / "unused" / ".." / "escape"
    with pytest.raises(module.ArtifactPreflightError):
        module.stage_scorecard_pair(
            scorecard=_scorecard(),
            output_dir=traversal,
            h5_port=ContractFixtureH5Port(),
        )


def test_symlink_output_parent_and_pair_member_fail_closed(tmp_path):
    module = _artifacts()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(module.ArtifactPreflightError):
        module.stage_scorecard_pair(
            scorecard=_scorecard(),
            output_dir=linked_parent / "scorecard-pair",
            h5_port=ContractFixtureH5Port(),
        )

    output = tmp_path / "scorecard-pair"
    output.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}\n")
    (output / "scorecard.json").symlink_to(target)
    (output / "scorecard.md").write_text("# fixture\n")
    with pytest.raises(module.ArtifactVerificationError):
        module.inspect_exact_artifact_replay(
            scorecard=_scorecard(), output_dir=output, h5_port=ContractFixtureH5Port()
        )


def test_cross_filesystem_device_mutant_preserves_fresh_staging(tmp_path, monkeypatch):
    module = _artifacts()
    real_device = module._device_id

    def mismatched_device(path):
        value = real_device(path)
        return value + 1 if path.name.startswith(".scorecard-pair.staging-") else value

    monkeypatch.setattr(module, "_device_id", mismatched_device)
    output = tmp_path / "scorecard-pair"
    with pytest.raises(module.ArtifactStageError, match="cross-filesystem") as raised:
        module.stage_scorecard_pair(
            scorecard=_scorecard(), output_dir=output, h5_port=ContractFixtureH5Port()
        )
    assert raised.value.state.staging_dir is not None
    assert raised.value.state.staging_dir.exists()
    assert not output.exists()


def test_concurrent_creator_at_rename_is_not_overwritten_and_stage_survives(
    tmp_path, monkeypatch
):
    module, port, _score, output, staged = _stage(tmp_path)

    def concurrent_creator(_source, destination):
        destination.mkdir()
        raise OSError(errno.EEXIST, "simulated no-replace race")

    monkeypatch.setattr(module, "_rename_noreplace", concurrent_creator)
    with pytest.raises(module.ArtifactPublishError) as raised:
        module.publish_staged_pair(staged, h5_port=port)
    assert raised.value.state.stage == "RENAME_FAILED_STAGING_PRESERVED"
    assert raised.value.state.renamed is False
    assert staged.staging_dir.exists()
    assert output.exists()
    assert tuple(output.iterdir()) == ()


def test_parent_fsync_failure_preserves_only_real_final_pair_state(
    tmp_path, monkeypatch
):
    module, port, _score, output, staged = _stage(tmp_path)

    def fail_parent_fsync(path):
        assert path == output.parent
        raise OSError("simulated parent fsync failure")

    monkeypatch.setattr(module, "_fsync_directory", fail_parent_fsync)
    with pytest.raises(module.ArtifactPublishError) as raised:
        module.publish_staged_pair(staged, h5_port=port)
    assert raised.value.state.stage == "PARENT_FSYNC_FAILED"
    assert raised.value.state.renamed is True
    assert raised.value.state.staging_dir is None
    assert raised.value.state.final_dir == output
    assert output.exists()
    assert not staged.staging_dir.exists()
    assert sorted(path.name for path in output.iterdir()) == [
        "scorecard.json",
        "scorecard.md",
    ]


@pytest.mark.parametrize("shape", ("json_only", "markdown_only", "extra"))
def test_half_pair_and_extra_final_file_never_count_as_replay(tmp_path, shape):
    module = _artifacts()
    output = tmp_path / "scorecard-pair"
    output.mkdir()
    if shape != "markdown_only":
        (output / "scorecard.json").write_bytes(b"{}\n")
    if shape != "json_only":
        (output / "scorecard.md").write_bytes(b"# fixture\n")
    if shape == "extra":
        (output / "extra.txt").write_text("forensic")
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(module.ArtifactVerificationError):
        module.inspect_exact_artifact_replay(
            scorecard=_scorecard(), output_dir=output, h5_port=ContractFixtureH5Port()
        )
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_exact_replay_is_read_only_no_stage_delete_or_publish(tmp_path, monkeypatch):
    module, port, scorecard, output, staged = _stage(tmp_path)
    module.publish_staged_pair(staged, h5_port=port)
    before_listing = tuple(sorted(path.name for path in tmp_path.iterdir()))
    before = {
        path.name: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in output.iterdir()
    }

    def poison(*_args, **_kwargs):
        raise AssertionError("replay attempted a mutation")

    monkeypatch.setattr(module, "_write_exclusive_fsynced", poison)
    monkeypatch.setattr(module, "_rename_noreplace", poison)
    monkeypatch.setattr(module.tempfile, "mkdtemp", poison)
    inspection = module.inspect_exact_artifact_replay(
        scorecard=scorecard, output_dir=output, h5_port=ContractFixtureH5Port()
    )
    assert inspection.disposition == "EXACT_ARTIFACT_REPLAY"
    assert tuple(sorted(path.name for path in tmp_path.iterdir())) == before_listing
    assert {
        path.name: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in output.iterdir()
    } == before


def test_differing_existing_pair_is_read_only_collision(tmp_path):
    module, port, scorecard, output, staged = _stage(tmp_path)
    module.publish_staged_pair(staged, h5_port=port)
    json_path = output / "scorecard.json"
    json_path.write_bytes(json_path.read_bytes().replace(b"fixture", b"changed", 1))
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(module.ArtifactVerificationError):
        module.inspect_exact_artifact_replay(
            scorecard=scorecard,
            output_dir=output,
            h5_port=ContractFixtureH5Port(),
        )
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_artifact_module_has_no_delete_or_two_file_publish_path():
    source = (
        Path(__file__).resolve().parents[1] / "rob974_h6b_artifacts.py"
    ).read_text()
    assert "shutil" not in source
    assert "unlink(" not in source
    assert "rmtree" not in source
    assert source.count("_rename_noreplace(staged.staging_dir, staged.final_dir)") == 1

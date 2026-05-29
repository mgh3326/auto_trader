import importlib

import artifact_paths


def test_pit_data_root_defaults_to_repo_data(monkeypatch):
    monkeypatch.delenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", raising=False)
    importlib.reload(artifact_paths)
    root = artifact_paths.pit_data_root()
    assert root.name == "data"
    assert root.parent.name == "nautilus_scalping"


def test_pit_data_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", str(tmp_path))
    root = artifact_paths.pit_data_root()
    assert root == tmp_path / "data"


def test_pit_data_root_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv("AUTO_TRADER_RESEARCH_ARTIFACT_ROOT", "   ")
    root = artifact_paths.pit_data_root()
    assert root.name == "data" and root.parent.name == "nautilus_scalping"

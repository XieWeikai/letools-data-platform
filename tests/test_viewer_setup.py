"""Exercise Git's real nested-checkout behavior and stale-cache repair."""
import importlib.util
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def setup_module():
    file = Path(__file__).resolve().parents[1] / "setup_viewer.py"
    spec = importlib.util.spec_from_file_location("platform_setup_under_test", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_git_apply_modifies_only_nested_cache_and_restores_environment(tmp_path, monkeypatch, setup_module):
    repository = tmp_path / "platform"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "example.txt").write_text("parent must remain untouched\n")
    state = repository / ".state"
    stage = state / "viewer-upstream/staging"
    stage.mkdir(parents=True)
    (stage / "example.txt").write_text("before\n")
    patch = b"diff --git a/example.txt b/example.txt\n--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-before\n+after\n"
    monkeypatch.setenv("GIT_DIR", str(repository / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repository))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repository / ".git/index"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    previous = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
    with pytest.raises(RuntimeError, match="restore on failure"):
        with setup_module.private_git_context(state):
            subprocess.run(["git", "apply"], input=patch, cwd=stage, check=True)
            raise RuntimeError("restore on failure")
    assert (stage / "example.txt").read_text() == "after\n"
    assert (repository / "example.txt").read_text() == "parent must remain untouched\n"
    assert {key: value for key, value in os.environ.items() if key.startswith("GIT_")} == previous


@pytest.mark.parametrize("repair_works", [True, False])
def test_stale_doctor_patch_marker_is_not_trusted(tmp_path, monkeypatch, setup_module, repair_works):
    config = SimpleNamespace(state_dir=tmp_path / "state")
    source = config.state_dir / "viewer-upstream/source"
    file = source / "src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    file.parent.mkdir(parents=True)
    file.write_text("unpatched external Doctor")
    calls = []

    def fake_prepare(**kwargs):
        calls.append(kwargs)
        if kwargs.get("force") and repair_works:
            file.write_text("process.env.NEXT_PUBLIC_LEROBOT_DOCTOR_URL href={doctorUrl} src={doctorUrl}")
        return SimpleNamespace(source=source)

    monkeypatch.setattr("letools.visualizer.prepare_visualizer", fake_prepare)
    if repair_works:
        assert setup_module.prepare_upstream(config).source == source
    else:
        with pytest.raises(RuntimeError, match="missing.*local Doctor"):
            setup_module.prepare_upstream(config)
    assert len(calls) == 2
    assert calls[1] == {"cache_dir": config.state_dir / "viewer-upstream",
                        "install_dependencies": False, "force": True}

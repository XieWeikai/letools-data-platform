import stat

from data_web_platform.config import Config


def test_explicit_runtime_paths_override_derived_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DWP_PROJECT_ROOT", str(tmp_path / "checkout"))
    monkeypatch.setenv("DWP_LETOOLS_ROOT", str(tmp_path / "core"))
    monkeypatch.setenv("DWP_STATE_DIR", str(tmp_path / "separate-state"))
    monkeypatch.setenv("DWP_PYTHON", str(tmp_path / "private-venv/bin/python"))
    config = Config.from_env()
    assert config.project_root == tmp_path / "checkout"
    assert config.state_dir == tmp_path / "separate-state"
    assert config.python == tmp_path / "private-venv/bin/python"


def test_initialize_repairs_existing_private_state_permissions(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o775)
    for name in ("jobs", "config", "outputs", "exports"):
        (state / name).mkdir(mode=0o775)
        (state / name).chmod(0o775)
    providers = state / "config/providers.toml"
    providers.write_text("# preserve existing configuration\n[providers]\n")
    providers.chmod(0o664)
    unrelated = tmp_path / "source-dataset"
    unrelated.mkdir(mode=0o755)
    unrelated.chmod(0o755)

    config = Config(project_root=tmp_path, state_dir=state)
    config.initialize()

    for path in [state, *(state / name for name in ("jobs", "config", "outputs", "exports"))]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in [providers, state / "internal-token"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (state / "access-token").exists()
    assert config.internal_token
    token = config.internal_token
    config.initialize()
    assert config.internal_token == token
    assert providers.read_text() == "# preserve existing configuration\n[providers]\n"
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o755

"""All user-facing path flows use OS permissions, not configured mount lists."""
import errno
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from data_web_platform.app import create_app
from data_web_platform.config import Config
from data_web_platform.datasets import DatasetRegistry, dataset_fingerprint, inspect_dataset
from data_web_platform.doctor import prepare_doctor_request
from data_web_platform.doctor_worker import _validated_request
from data_web_platform.editor import prepare_editor_request
from data_web_platform.editor_worker import run_prepared as run_editor
from data_web_platform.paths import browse_directory, validate_path, validate_output_path
from data_web_platform.raw_data import source_snapshot
from data_web_platform.raw_viewer import RawViewer
from data_web_platform.viewer import _validated_path
from test_paths import make_dataset
from test_raw_viewer import recording


@pytest.fixture
def filesystem(tmp_path, monkeypatch):
    # Source/output deliberately live outside the project and pretend home.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DWP_PROJECT_ROOT", str(home / "platform"))
    monkeypatch.setenv("DWP_STATE_DIR", str(home / "platform/state"))
    monkeypatch.setenv("DWP_ALLOWED_ROOTS", str(tmp_path / "offline-disk"))
    return Config.from_env(), tmp_path / "separate-mount"


@pytest.fixture
def client(filesystem):
    config, _ = filesystem
    with patch("data_web_platform.app.discover_providers", return_value=[
            {"name": "tianji", "web_supported": True}]):
        with TestClient(create_app(config, start_services=False)) as test:
            yield test


def test_legacy_environment_does_not_limit_paths_or_probe_mounts(filesystem, client, monkeypatch):
    config, _ = filesystem
    assert config.allowed_roots is None
    assert config.default_source == str(Path.home())
    # An environment request must not trigger any mount filesystem probe.
    def offline(*args, **kwargs):
        raise OSError(errno.ENODEV, "No such device", "/offline-disk")
    monkeypatch.setattr(Path, "is_dir", offline)
    response = client.get("/api/environment")
    assert response.status_code == 200
    body = response.json()
    assert body["path_access_mode"] == "os_permissions"
    assert body["service_user"]
    assert body["roots"] == [str(Path.home()), "/"]
    assert body["providers"][0]["name"] == "tianji"


def test_browse_prefix_create_and_parent_on_arbitrary_filesystem(filesystem, client):
    config, mount = filesystem
    (mount / "pick_and_place").mkdir(parents=True)
    (mount / "peg_in_hole").mkdir()
    result = client.get("/api/browse", params={"path": str(mount)})
    assert result.status_code == 200, result.text
    assert result.json()["parent"] == str(mount.parent)
    suggested = client.get("/api/directories/suggest", params={"path": str(mount / "pi")})
    assert [item["name"] for item in suggested.json()["dirs"]] == ["pick_and_place"]
    created = client.post("/api/directories", json={"parent": str(mount), "name": "output-parent"})
    assert created.status_code == 201, created.text
    assert Path(created.json()["path"]).is_dir()
    assert browse_directory("/")["parent"] is None


def test_recording_selection_and_conversion_request_outside_home(filesystem, client):
    _, mount = filesystem
    raw = recording(mount)
    listing = client.get("/api/recordings", params={"path": str(mount)})
    assert listing.status_code == 200, listing.text
    assert listing.json()["recordings"][0]["path"] == str(raw)
    destination = mount / "converted"
    result = client.post("/api/jobs", json={"kind": "convert", "source_root": str(raw),
        "recordings": [str(raw)], "destination": str(destination)})
    assert result.status_code == 202, result.text
    assert result.json()["request"]["destination"] == str(destination)
    assert not destination.exists()


@pytest.mark.parametrize("error,code,message", [
    (PermissionError(errno.EACCES, "Permission denied", "/private-dir"), 403, "没有访问权限"),
    (OSError(errno.ENODEV, "No such device", "/offline-disk"), 503, "文件系统暂时不可用"),
    (OSError(errno.ESTALE, "Stale file handle", "/stale-nas"), 503, "文件系统暂时不可用"),
])
def test_permissions_and_mount_failures_are_actionable(client, monkeypatch, error, code, message):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr("data_web_platform.app.browse_directory", fail)
    response = client.get("/api/browse", params={"path": error.filename})
    assert response.status_code == code
    assert message in response.json()["detail"]
    assert error.filename in response.json()["detail"]


def test_path_resolution_preserves_os_errors(monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", "/private-dir")
    monkeypatch.setattr(Path, "resolve", fail)
    with pytest.raises(PermissionError):
        validate_path("/private-dir")


def test_registration_doctor_editor_and_viewer_use_same_policy(filesystem, client, tmp_path):
    config, mount = filesystem
    source = make_dataset(mount / "dataset")
    registered = client.post("/api/datasets", json={"path": str(source)})
    assert registered.status_code == 201, registered.text
    registry = DatasetRegistry(config)
    record = registry.get(registered.json()["id"])
    doctor = prepare_doctor_request({"dataset": record["id"]}, registry, config)
    assert doctor["allowed_roots"] is None
    _, roots, sources = _validated_request(json.loads(json.dumps(doctor)))
    assert roots is None and sources == [source]
    missing_policy = dict(doctor)
    missing_policy.pop("allowed_roots")
    with pytest.raises(ValueError, match="访问策略"):
        _validated_request(missing_policy)
    output = mount / "edited"
    editor = prepare_editor_request({"dataset": record["id"], "output": str(output),
        "operation": "plan", "source_fingerprint": dataset_fingerprint(source),
        "task_by_episode": {"0": "Test task"}}, registry, config, SimpleNamespace())
    assert editor["allowed_roots"] is None
    workspace = tmp_path / "editor-workspace"
    workspace.mkdir()
    assert run_editor(json.loads(json.dumps(editor)), workspace) == 0
    assert not output.exists()  # A preview must not create a final dataset.
    assert _validated_path(config, source) == source
    assert _validated_path(config, mount / "export", must_exist=False) == mount / "export"


def test_raw_viewer_worker_request_has_no_directory_jail(filesystem, monkeypatch):
    config, mount = filesystem
    raw = recording(mount)
    service = RawViewer(config)
    monkeypatch.setattr(service.executor, "submit", lambda *args: None)
    try:
        status = service.open(raw)
        request = json.loads((service.root / status["id"] / "request.json").read_text())
        assert request["roots"] is None
        assert source_snapshot(raw, request["roots"]) == request["snapshot"]
    finally:
        service.close()


def test_os_access_keeps_dataset_containment_and_no_overwrite(filesystem):
    _, mount = filesystem
    source = make_dataset(mount / "dataset")
    alias = mount / "alias"
    alias.symlink_to(source, target_is_directory=True)
    assert validate_path(alias) == source
    for destination in (source, source / "inside", mount):
        with pytest.raises(ValueError):
            validate_output_path(destination, inputs=[source])
    (source / "external").symlink_to(Path.home(), target_is_directory=True)
    with pytest.raises(ValueError, match="数据范围"):
        inspect_dataset(source, None)


def test_raw_mcap_manifest_cannot_escape_recording_in_os_mode(filesystem):
    _, mount = filesystem
    raw = recording(mount)
    (mount / "external.mcap").write_bytes(b"outside recording")
    (raw / "data/record.mcap").unlink()
    (raw / "data/record.mcap").symlink_to(mount / "external.mcap")
    with pytest.raises(ValueError, match="MCAP 清单路径"):
        source_snapshot(raw, None)


def test_user_service_shares_normal_temporary_filesystem():
    unit = (Path(__file__).resolve().parents[1] / "deploy/data-web-platform.service").read_text()
    assert "PrivateTmp=false" in unit
    assert "NoNewPrivileges=true" in unit
    assert "DWP_ALLOWED_ROOTS=" not in unit

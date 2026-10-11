from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from data_web_platform.app import create_app, prepare_job
from data_web_platform.config import Config
from data_web_platform.models import AlignmentParams, JobRequest


@pytest.fixture
def config(tmp_path):
    return Config(project_root=tmp_path, state_dir=tmp_path / "state",
                  letools_root=tmp_path / "letools", allowed_roots=[tmp_path])


@pytest.fixture
def client(config):
    with patch("data_web_platform.app.discover_providers", return_value=[
            {"name": "tianji", "web_supported": True, "version": "0.4.0"}]):
        with TestClient(create_app(config, start_services=False)) as test:
            yield test


def test_direct_access_without_login(client):
    assert not client.cookies
    response = client.get("/")
    assert response.status_code == 200
    assert "数据处理工作台" in response.text
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/environment").json()["providers"][0]["name"] == "tianji"
    assert client.get("/api/datasets").status_code == 200
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/doctor").status_code == 200
    assert not client.cookies


def test_old_login_url_redirects_to_dashboard(client):
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get("/login").status_code == 200
    assert client.get("/static/login.html").status_code == 404
    assert client.post("/api/session", json={"token": "obsolete"}).status_code == 404


def test_cross_site_write_protection_without_login(client):
    for origin in ("https://evil.example", "null", "https://testserver"):
        assert client.post("/api/jobs", json={}, headers={"origin": origin}).status_code == 403
    # A same-origin request reaches normal payload validation, not a login gate.
    assert client.post("/api/jobs", json={}, headers={"origin": "http://testserver"}).status_code == 422


def test_old_cookie_cannot_restore_login_requirement(client):
    client.cookies.set("dwp_session", "expired-or-invalid")
    assert client.get("/").status_code == 200
    assert client.get("/api/environment").status_code == 200
    assert client.get("/doctor").status_code == 200


def recording(path):
    (path / "data").mkdir(parents=True)
    (path / "data/metadata.yaml").write_text("rosbag2_bagfile_information: {}")
    return str(path)


def test_select_and_new_outputs(client, config):
    root = config.project_root / "raw"
    one = recording(root / "one")
    two = recording(root / "two")
    listing = client.get("/api/recordings", params={"path": str(root)}).json()
    assert len(listing["recordings"]) == 2
    payload = {"kind": "convert", "source_root": str(root), "recordings": [one],
               "destination": str(config.project_root / "output")}
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["request"]["recordings"] == [one]
    assert job["request"]["params"]["fps"] == 50
    assert job["request"]["params"]["task"] == "Plug in the Ethernet cable"
    assert client.post("/api/jobs", json=payload).status_code == 400  # reserved
    assert client.post(f'/api/jobs/{job["id"]}/cancel').json()["status"] == "cancelled"
    payload["recordings"] = [one, one]
    assert client.post("/api/jobs", json=payload).status_code == 400
    payload["recordings"] = [one]
    payload["destination"] = str(root / "nested-output")
    assert client.post("/api/jobs", json=payload).status_code == 400
    payload["destination"] = str(config.project_root)
    assert client.post("/api/jobs", json=payload).status_code == 400
    assert client.get("/api/browse", params={"path": "/etc"}).status_code == 400


def test_params_reject_unsupported_and_nonfinite():
    for bad in ({"fps": 0}, {"task": " "}, {"max_gap_ms": float("nan")},
                {"command_max_age_ms": 1001}, {"state_resampling": "nearest"}, {"split": True}):
        with pytest.raises(ValueError):
            AlignmentParams(**bad)


def test_incomplete_recordings_are_not_silently_filtered(client, config):
    root = config.project_root / "raw"
    root.mkdir()
    empty = root / "my_bag-broken"
    empty.mkdir()
    video_only = root / "video-only"
    (video_only / "video").mkdir(parents=True)
    (root / "unrelated").mkdir()
    listing = client.get("/api/recordings", params={"path": str(root)}).json()
    assert {p["path"] for p in listing["recordings"]} == {str(empty), str(video_only)}
    assert all(p["incomplete"] for p in listing["recordings"])
    direct = client.get("/api/recordings", params={"path": str(empty)}).json()
    assert len(direct["recordings"]) == 1
    request = JobRequest(kind="check", source_root=str(root), recordings=[str(empty)])
    assert prepare_job(request, config, [{"name": "tianji", "web_supported": True}])["recordings"] == [str(empty)]


def test_config_override_derives_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("DWP_PROJECT_ROOT", str(tmp_path / "platform"))
    monkeypatch.setenv("DWP_LETOOLS_ROOT", str(tmp_path / "letools"))
    c = Config.from_env()
    assert c.state_dir == tmp_path / "platform/.state"
    assert c.python == tmp_path / "letools/.venv/bin/python"


def test_provider_restricted(config):
    root = config.project_root / "raw"
    path = recording(root / "one")
    request = JobRequest(kind="check", source_root=str(root), recordings=[path], provider="apex_rosbag")
    with pytest.raises(ValueError, match="provider"):
        prepare_job(request, config, [{"name": "tianji", "web_supported": True}])

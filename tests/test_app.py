import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from data_web_platform.app import create_app, prepare_job, session_value, valid_session
from data_web_platform.config import Config
from data_web_platform.models import AlignmentParams, JobRequest


@pytest.fixture
def config(tmp_path):
    return Config(project_root=tmp_path, state_dir=tmp_path / "state",
                  letools_root=tmp_path / "letools", allowed_roots=[tmp_path],
                  auth_token="test-secret-not-for-production")


@pytest.fixture
def client(config):
    with patch("data_web_platform.app.discover_providers", return_value=[
            {"name": "tianji", "web_supported": True, "version": "0.4.0"}]):
        with TestClient(create_app(config, start_services=False)) as test:
            yield test


def test_auth_and_cross_site(client):
    assert client.get("/api/environment").status_code == 401
    assert client.post("/api/session", json={"token": "incorrect"}).status_code == 401
    response = client.post("/api/session", json={"token": "test-secret-not-for-production"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/api/environment").json()["providers"][0]["name"] == "tianji"
    assert client.post("/api/jobs", json={}, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.delete("/api/session").status_code == 200
    assert not client.get("/api/session").json()["authenticated"]


def test_cookie_signature_expiry():
    assert valid_session("x", session_value("x"))
    assert not valid_session("x", session_value("y"))
    assert not valid_session("x", session_value("x", int(time.time()) - 10))
    assert not valid_session("x", "garbage")
    assert not valid_session("x", None)


def recording(path):
    (path / "data").mkdir(parents=True)
    (path / "data/metadata.yaml").write_text("rosbag2_bagfile_information: {}")
    return str(path)


def test_select_and_new_outputs(client, config):
    client.post("/api/session", json={"token": config.auth_token})
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
    client.post("/api/session", json={"token": config.auth_token})
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

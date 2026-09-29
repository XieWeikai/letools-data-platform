from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data_web_platform.config import Config
from data_web_platform import viewer as viewer_module
from data_web_platform.viewer import (
    AnnotationStore, DataBridge, DatasetRef, ViewerService, create_viewer_router,
    dataset_file, _byte_range,
)

PROJECT = Path(__file__).resolve().parents[1]
try:
    from letools.external import visualizer_source
    UPSTREAM = visualizer_source()
    LETOOLS = UPSTREAM.parents[2]
except ImportError:
    LETOOLS = Path("/Users/xieweikai/other/letools")


class Registry:
    def __init__(self):
        self.records = []

    def register(self, path):
        record = {"id": Path(path).name, "repo_id": "local/" + Path(path).name,
                  "path": str(path), "name": Path(path).name,
                  "info": json.loads((Path(path) / "meta/info.json").read_text())}
        self.records.append(record)
        return record

    def get(self, identity):
        for record in self.records:
            if identity in (record["id"], record["repo_id"]):
                return record
        raise KeyError(identity)

    def all(self):
        return self.records.copy()


def make_dataset(root, version="v2.1"):
    (root / "meta").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    (root / "videos/camera").mkdir(parents=True)
    (root / "videos/camera/clip.mp4").write_bytes(bytes(range(256)) * 10)
    info = {"codebase_version": version, "fps": 50, "chunks_size": 1000,
            "total_episodes": 2, "total_frames": 6,
            "features": {"observation.state": {"dtype": "float32", "shape": [1]},
                         "observation.images.head_left": {"dtype": "video", "shape": [3, 2, 2]}}}
    tables = [pa.table({"episode_index": [episode] * 3, "timestamp": [0., .02, .04],
                       "frame_index": [0, 1, 2], "observation.state": [[0.], [.1], [.2]]})
              for episode in range(2)]
    if version == "v2.1":
        info["data_path"] = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
        (root / "meta/episodes.jsonl").write_text("\n".join(json.dumps({"episode_index": episode, "length": 3, "tasks": ["test"]}) for episode in range(2)))
        for episode, table in enumerate(tables):
            pq.write_table(table, root / f"data/chunk-000/episode_{episode:06d}.parquet")
    else:
        info["data_path"] = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
        (root / "meta/episodes/chunk-000").mkdir(parents=True)
        pq.write_table(pa.table({"episode_index": [0, 1], "length": [3, 3],
                                "data/chunk_index": [0, 0], "data/file_index": [0, 0]}),
                       root / "meta/episodes/chunk-000/file-000.parquet")
        pq.write_table(pa.concat_tables(tables), root / "data/chunk-000/file-000.parquet")
    (root / "meta/info.json").write_text(json.dumps(info))
    return root


@pytest.fixture
def setup(tmp_path):
    config = Config(project_root=PROJECT, state_dir=tmp_path / "state", letools_root=LETOOLS,
                    allowed_roots=[tmp_path], viewer_port=3010)
    config.initialize()
    registry = Registry()
    records = [registry.register(make_dataset(tmp_path / name, version)) for name, version in
               [("v21", "v2.1"), ("v30", "v3.0")]]
    service = ViewerService(config, registry)
    app = FastAPI()
    router = create_viewer_router(config, registry, service=service)
    app.include_router(router)
    with TestClient(app) as client:
        yield SimpleNamespace(config=config, registry=registry, records=records, service=service,
                              store=AnnotationStore(config, registry), client=client, app=app)


def atom(**changes):
    return {"role": "assistant", "content": "insert cable", "style": "subtask", "timestamp": 0., **changes}


def save(client, repo, episode=0, atoms=None):
    return client.post(f"/api/episodes/{episode}/atoms", json={"repo_id": repo, "episode_index": episode,
                                                            "atoms": atoms if atoms is not None else [atom()]})


@pytest.mark.parametrize("repo", ["local/v21", "local/v30"])
def test_load_save_events_and_reload(setup, repo):
    client = setup.client
    response = client.post("/api/dataset/load", json={"repo_id": repo})
    assert response.status_code == 200, response.text
    assert response.json()["num_episodes"] == 2
    assert response.json()["fps"] == 50
    ts = client.get("/api/episodes/1/frame_timestamps", params={"repo_id": repo})
    assert ts.json()["timestamps"] == [0., .02, .04]
    response = save(client, repo, atoms=[atom(timestamp=.011), atom(style="interjection", timestamp=.033)])
    assert response.status_code == 200, response.text
    values = client.get("/api/episodes/0/atoms", params={"repo_id": repo}).json()["atoms"]
    assert [v["timestamp"] for v in values] == [.011, .04]
    # New store instance must pick up atomically persisted state.
    record = setup.registry.get(repo)
    store = AnnotationStore(setup.config, setup.registry)
    assert store.atoms(store.load(record), 0) == values
    assert not list((Path(record["path"]) / "meta").glob(".annotations-*"))


def test_scoped_identity_and_validation(setup, tmp_path):
    client = setup.client
    assert client.post("/api/dataset/load", json={"repo_id": "local/nope"}).status_code == 404
    assert client.post("/api/dataset/load", json={"local_path": str(tmp_path)}).status_code == 404
    assert client.post("/api/dataset/load", json={"local_path": "/etc"}).status_code == 403
    assert client.post("/api/dataset/load", json={"repo_id": "local/v21", "local_path": setup.records[1]["path"]}).status_code == 400
    assert client.post("/api/dataset/load", json={"repo_id": "local/v21", "revision": "dev"}).status_code == 400
    assert client.get("/api/episodes/99/frame_timestamps?repo_id=local/v21").status_code == 404
    assert save(client, "local/v21", atoms=[atom(style="wrong")]).status_code == 400
    assert save(client, "local/v21", atoms=[atom(style="vqa", camera="missing")]).status_code == 400
    assert save(client, "local/v21", atoms=[atom(timestamp=5)]).status_code == 400
    assert save(client, "local/v21", atoms=[atom(timestamp=-1)]).status_code == 422
    assert save(client, "local/v21", atoms=[atom(style="vqa", camera="observation.images.head_left")]).status_code == 200
    nan_payload = '{"repo_id":"local/v21","episode_index":0,"atoms":[{"role":"assistant","content":"x","style":"subtask","timestamp":NaN}]}'
    # FastAPI's default validation error handler itself cannot serialize NaN;
    # model validation is checked directly instead of sending invalid JSON.
    from data_web_platform.viewer import AtomsPayload
    with pytest.raises(ValueError):
        AtomsPayload.model_validate_json(nan_payload)


def test_concurrent_saves_preserve_other_episode(setup):
    barrier = threading.Barrier(2)
    record = setup.records[0]
    def task(episode):
        # Independent stores emulate separate platform worker instances.
        store = AnnotationStore(setup.config, setup.registry)
        barrier.wait(timeout=5)
        with store.locked(Path(record["path"])):
            store.save(store.load(record), episode, [atom(content=f"episode {episode}")])
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(task, [0, 1]))
    state = setup.store.load(record)
    assert state.annotations[0].atoms[0]["content"] == "episode 0"
    assert state.annotations[1].atoms[0]["content"] == "episode 1"


@pytest.mark.parametrize("repo", ["local/v21", "local/v30"])
@pytest.mark.parametrize("symlink", ["annotation", "meta"])
def test_annotation_save_refuses_internal_symlinks_without_changing_info(setup, repo, symlink):
    root = Path(setup.registry.get(repo)["path"])
    info = (root / "meta/info.json").read_bytes()
    original_files = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    if symlink == "annotation":
        (root / "meta/lerobot_annotations.json").symlink_to("info.json")
        metadata = root / "meta"
    else:
        metadata = root / "real-meta"
        (root / "meta").rename(metadata)
        (root / "meta").symlink_to(metadata, target_is_directory=True)
    response = save(setup.client, repo)
    assert response.status_code == 403, response.text
    assert (root / "meta/info.json").read_bytes() == info
    for relative, content in original_files.items():
        assert (root / relative).read_bytes() == content
    assert not list(metadata.glob(".annotations-*"))
    if symlink == "annotation":
        assert (metadata / "lerobot_annotations.json").is_symlink()
    else:
        assert not (metadata / "lerobot_annotations.json").exists()


def test_annotation_save_rechecks_symlink_created_during_write(setup, monkeypatch):
    root = Path(setup.records[0]["path"])
    info = (root / "meta/info.json").read_bytes()
    original_check = viewer_module._check_annotation_target
    calls = 0

    def insert_symlink(meta_fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            os.symlink("info.json", "lerobot_annotations.json", dir_fd=meta_fd)
        original_check(meta_fd)

    monkeypatch.setattr(viewer_module, "_check_annotation_target", insert_symlink)
    response = save(setup.client, "local/v21")
    assert response.status_code == 403, response.text
    assert calls == 2
    assert (root / "meta/info.json").read_bytes() == info
    assert (root / "meta/lerobot_annotations.json").is_symlink()
    assert not list((root / "meta").glob(".annotations-*"))


def test_annotation_replace_is_dirfd_scoped_even_after_last_instant_symlink(setup, monkeypatch):
    root = Path(setup.records[0]["path"])
    info = (root / "meta/info.json").read_bytes()
    original_replace = os.replace
    calls = []

    def replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        assert destination == "lerobot_annotations.json"
        assert source.startswith(".annotations-") and "/" not in source
        assert src_dir_fd is not None and src_dir_fd == dst_dir_fd
        calls.append(destination)
        os.symlink("info.json", destination, dir_fd=dst_dir_fd)
        return original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(viewer_module.os, "replace", replace)
    response = save(setup.client, "local/v21")
    assert response.status_code == 200, response.text
    assert calls == ["lerobot_annotations.json"]
    assert (root / "meta/info.json").read_bytes() == info
    assert not (root / "meta/lerobot_annotations.json").is_symlink()
    assert json.loads((root / "meta/lerobot_annotations.json").read_text())["episodes"]["0"]["atoms"]


@pytest.mark.skipif(sys.platform != "linux", reason="exports use Linux atomic no-replace publication")
@pytest.mark.parametrize("repo", ["local/v21", "local/v30"])
def test_export_is_materialized_and_source_untouched(setup, tmp_path, repo):
    client = setup.client
    root = Path(setup.registry.get(repo)["path"])
    source_hashes = {p: p.read_bytes() for p in (root / "data").rglob("*.parquet")}
    save(client, repo, atoms=[atom(), atom(style="interjection", timestamp=.019)])
    output = tmp_path / (root.name + "-export")
    response = client.post("/api/export", json={"repo_id": repo, "output_dir": str(output), "copy_videos": True})
    assert response.status_code == 200, response.text
    assert response.json()["persistent_rows"] == 1
    assert response.json()["event_rows"] == 1
    assert json.loads((output / "meta/info.json").read_text())["codebase_version"] == setup.registry.get(repo)["info"]["codebase_version"]
    assert (output / "videos/camera/clip.mp4").read_bytes() == (root / "videos/camera/clip.mp4").read_bytes()
    assert not (output / "videos").is_symlink()
    for path, original in source_hashes.items():
        assert path.read_bytes() == original
        assert "language_persistent" in pq.read_table(output / path.relative_to(root)).column_names
    exported = setup.registry.register(output)
    state = setup.store.load(exported)
    # Export preserves the sidecar and also materializes language into Parquet.
    (output / "meta/lerobot_annotations.json").unlink()
    state = setup.store.load(exported)
    atoms = setup.store.atoms(state, 0)
    assert len(atoms) == 2
    assert next(a for a in atoms if a["style"] == "interjection")["timestamp"] == .02
    assert client.post("/api/export", json={"repo_id": repo, "output_dir": str(output)}).status_code in (400, 409)


def test_export_destination_and_symlink_guards(setup, tmp_path):
    client, root = setup.client, Path(setup.records[0]["path"])
    for output in (root, root / "nested", tmp_path, Path(setup.records[1]["path"])):
        assert client.post("/api/export", json={"repo_id": "local/v21", "output_dir": str(output)}).status_code == 400
    assert client.post("/api/export", json={"repo_id": "local/v21", "output_dir": "/etc/invalid-export"}).status_code == 403
    (root / "videos/camera/escape.mp4").symlink_to("/etc/hosts")
    output = tmp_path / "bad-export"
    response = client.post("/api/export", json={"repo_id": "local/v21", "output_dir": str(output)})
    assert response.status_code == 403
    assert not output.exists()
    assert not list(tmp_path.glob(".bad-export.dwp-*"))


@pytest.mark.skipif(sys.platform != "linux", reason="exports use Linux atomic no-replace publication")
@pytest.mark.parametrize("collision", ["empty-dir", "populated-dir", "file", "symlink"])
def test_export_never_replaces_a_concurrently_created_destination(setup, tmp_path, monkeypatch, collision):
    root = Path(setup.records[0]["path"])
    original_files = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    destination = tmp_path / "raced-export"
    original_publish = viewer_module.publish_noreplace
    calls = []

    def publish_with_collision(staging, target):
        assert target == destination
        calls.append(target)
        if collision in {"empty-dir", "populated-dir"}:
            target.mkdir()
            if collision == "populated-dir":
                (target / "existing.txt").write_text("keep existing data")
        elif collision == "file":
            target.write_text("keep existing data")
        else:
            target.symlink_to(root, target_is_directory=True)
        original_publish(staging, target)

    monkeypatch.setattr(viewer_module, "publish_noreplace", publish_with_collision)
    response = setup.client.post("/api/export", json={"repo_id": "local/v21", "output_dir": str(destination)})
    assert response.status_code == 409, response.text
    assert calls == [destination]
    if collision == "empty-dir":
        assert destination.is_dir() and list(destination.iterdir()) == []
    elif collision == "populated-dir":
        assert (destination / "existing.txt").read_text() == "keep existing data"
    elif collision == "file":
        assert destination.read_text() == "keep existing data"
    else:
        assert destination.is_symlink() and destination.resolve() == root
    for path, content in original_files.items():
        assert path.read_bytes() == content
    assert not list(tmp_path.glob(".raced-export.dwp-*"))


def test_saved_annotation_corruption_never_overwritten(setup):
    path = Path(setup.records[0]["path"]) / "meta/lerobot_annotations.json"
    path.write_text('{"episodes": []}')
    assert save(setup.client, "local/v21").status_code == 422
    assert path.read_text() == '{"episodes": []}'


def test_public_files_range_and_traversal(setup):
    base = "/datasets/local/v21/resolve/main/"
    response = setup.client.get(base + "videos/camera/clip.mp4", headers={"Range": "bytes=10-29"})
    assert response.status_code == 206
    assert response.content == bytes(range(10, 30))
    assert response.headers["content-range"] == "bytes 10-29/2560"
    assert setup.client.head(base + "videos/camera/clip.mp4").content == b""
    assert setup.client.get(base + ".env").status_code == 403
    assert setup.client.get(base + "meta/%2e%2e/%2e%2e/secret").status_code == 403
    assert setup.client.get(base + "meta/.hidden").status_code == 403
    assert setup.client.get(base + "meta/missing.json").status_code == 404
    root = Path(setup.records[0]["path"])
    (root / "meta/escape.json").symlink_to("/etc/hosts")
    assert setup.client.get(base + "meta/escape.json").status_code == 403


def test_private_bridge_auth_range_and_head(setup):
    bridge = DataBridge(setup.config, setup.registry)
    origin = bridge.start()
    try:
        url = origin + "/datasets/local/v21/resolve/main/videos/camera/clip.mp4"
        with httpx.Client(trust_env=False) as client:
            assert client.get(url).status_code == 403
            assert client.get(url, headers={"X-DWP-Internal-Token": "wrong"}).status_code == 403
            headers = {"X-DWP-Internal-Token": setup.config.internal_token, "Range": "bytes=-4"}
            response = client.get(url, headers=headers)
            assert response.status_code == 206 and response.content == bytes(range(252, 256))
            assert client.head(url, headers=headers).headers["content-length"] == "4"
            headers["Range"] = "bytes=99999-"
            response = client.get(url, headers=headers)
            assert response.status_code == 416 and response.headers["content-range"] == "bytes */2560"
    finally:
        bridge.stop()


@pytest.mark.parametrize("header,size,result", [(None, 0, (0, -1, False)), (None, 5, (0, 4, False)),
                                               ("bytes=1-", 5, (1, 4, True)), ("bytes=1-99", 5, (1, 4, True))])
def test_range_parser(header, size, result):
    assert _byte_range(header, size) == result


@pytest.mark.parametrize("repo", ["local/v21", "local/v30"])
def test_optional_progress_manifest_skips_absent_files_without_editing_dataset(setup, repo):
    root = Path(setup.registry.get(repo)["path"])
    info_path = root / "meta/info.json"
    # A dataset cannot advertise arbitrary root files to the local frontend.
    info = json.loads(info_path.read_text())
    info["_dwp_optional_files"] = ["secret.parquet", "sarm_progress.parquet"]
    info_path.write_text(json.dumps(info))
    original = info_path.read_bytes()
    url = f"/datasets/{repo}/resolve/main/meta/info.json"
    response = setup.client.get(url)
    assert response.status_code == 200
    assert response.json()["_dwp_optional_files"] == []
    assert response.json()["features"] == info["features"]
    assert response.headers["cache-control"] == "private, no-store"
    head = setup.client.head(url)
    assert head.content == b"" and head.headers["content-length"] == str(len(response.content))
    assert info_path.read_bytes() == original


@pytest.mark.parametrize("name", ["sarm_progress.parquet", "srm_progress.parquet"])
def test_optional_progress_existing_file_is_available_with_range(setup, name):
    root = Path(setup.records[0]["path"])
    file = root / name
    pq.write_table(pa.table({"episode_index": [0, 0], "progress": [0., 1.]}), file)
    base = "/datasets/local/v21/resolve/main/"
    assert setup.client.get(base + "meta/info.json").json()["_dwp_optional_files"] == [name]
    assert setup.client.get(base + name).content == file.read_bytes()
    response = setup.client.get(base + name, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and response.content == b"PAR1"
    assert dataset_file(setup.config, setup.registry, "local/v21", name) == file


def test_optional_progress_does_not_expand_root_or_symlink_access(setup):
    root = Path(setup.records[0]["path"])
    outside = root.parent / "outside.parquet"
    outside.write_bytes(b"private")
    (root / "sarm_progress.parquet").symlink_to(outside)
    (root / "srm_progress.parquet").mkdir()
    (root / "other.parquet").write_bytes(b"private")
    base = "/datasets/local/v21/resolve/main/"
    assert setup.client.get(base + "meta/info.json").json()["_dwp_optional_files"] == []
    for name in ["sarm_progress.parquet", "other.parquet", ".env", "srm_progress.parquet/child"]:
        assert setup.client.get(base + name).status_code == 403
    assert setup.client.get(base + "srm_progress.parquet").status_code == 404


def test_episode_manifest_lists_real_multifile_chunks_in_numeric_order(setup):
    root = Path(setup.records[1]["path"])
    expected = ["meta/episodes/chunk-000/file-000.parquet",
                "meta/episodes/chunk-000/file-003.parquet",
                "meta/episodes/chunk-002/file-009.parquet",
                "meta/episodes/chunk-002/file-010.parquet",
                "meta/episodes/chunk-1000/file-000.parquet"]
    for relative in reversed(expected[1:]):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"episode_index": [0], "length": [3]}), path)
    info_path = root / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["_dwp_episode_shards"] = ["/private/secret", "meta/episodes/chunk-000/file-999.parquet"]
    info_path.write_text(json.dumps(info))
    before = info_path.read_bytes()
    response = setup.client.get("/datasets/local/v30/resolve/main/meta/info.json")
    assert response.status_code == 200
    assert response.json()["_dwp_episode_shards"] == expected
    assert info_path.read_bytes() == before
    for key, value in info.items():
        if not key.startswith("_dwp_"):
            assert response.json()[key] == value
    # Discovery is live rather than retaining stale entries after a deletion.
    (root / expected[1]).unlink()
    assert setup.client.get("/datasets/local/v30/resolve/main/meta/info.json").json()["_dwp_episode_shards"] == [expected[0], *expected[2:]]


def test_episode_manifest_rejects_noncanonical_files_and_symlinks(setup):
    root = Path(setup.records[1]["path"])
    directory = root / "meta/episodes/chunk-000"
    for name in ["secret.parquet", "file-001.json", "file-x.parquet", "file-001.parquet\n", ".file-001.parquet"]:
        (directory / name).write_bytes(b"not metadata")
    (directory / "file-001.parquet").mkdir()
    outside = root.parent / "outside-episode.parquet"
    outside.write_bytes(b"private")
    (directory / "file-002.parquet").symlink_to(outside)
    (directory / "file-003.parquet").symlink_to(directory / "file-000.parquet")
    (root / "meta/episodes/chunk-004").symlink_to(directory, target_is_directory=True)
    (root / "meta/episodes/chunk-005").symlink_to(outside.parent, target_is_directory=True)
    extra = root / "meta/episodes/chunk-bad"
    extra.mkdir()
    (extra / "file-000.parquet").write_bytes(b"not metadata")
    info = setup.client.get("/datasets/local/v30/resolve/main/meta/info.json").json()
    assert info["_dwp_episode_shards"] == ["meta/episodes/chunk-000/file-000.parquet"]


def test_episode_manifest_does_not_follow_escaped_metadata_directory(setup):
    root = Path(setup.records[1]["path"])
    episodes = root / "meta/episodes"
    outside = root.parent / "external-episodes"
    episodes.rename(outside)
    episodes.symlink_to(outside, target_is_directory=True)
    response = setup.client.get("/datasets/local/v30/resolve/main/meta/info.json")
    assert response.status_code == 200
    assert response.json()["_dwp_episode_shards"] == []


def test_episode_manifest_is_empty_for_v21_even_with_untrusted_hint(setup):
    root = Path(setup.records[0]["path"])
    path = root / "meta/info.json"
    info = json.loads(path.read_text())
    info["_dwp_episode_shards"] = ["meta/episodes/chunk-000/file-000.parquet"]
    path.write_text(json.dumps(info))
    original = path.read_bytes()
    response = setup.client.get("/datasets/local/v21/resolve/main/meta/info.json")
    assert response.json()["_dwp_episode_shards"] == []
    assert path.read_bytes() == original


@pytest.mark.parametrize("repo, record_index", [("v21", 0), ("v30", 1)])
def test_bridge_uses_same_optional_progress_manifest_and_file_policy(setup, repo, record_index):
    root = Path(setup.records[record_index]["path"])
    (root / "srm_progress.parquet").write_bytes(b"PAR1example")
    bridge = DataBridge(setup.config, setup.registry)
    origin = bridge.start()
    base = f"/datasets/local/{repo}/resolve/main/"
    try:
        with httpx.Client(base_url=origin, trust_env=False) as client:
            assert client.get(base + "meta/info.json").status_code == 403
            headers = {"X-DWP-Internal-Token": setup.config.internal_token}
            response = client.get(base + "meta/info.json", headers=headers)
            assert response.json() == setup.client.get(base + "meta/info.json").json()
            assert response.json()["_dwp_optional_files"] == ["srm_progress.parquet"]
            assert response.json()["_dwp_episode_shards"] == ([] if repo == "v21" else ["meta/episodes/chunk-000/file-000.parquet"])
            head = client.head(base + "meta/info.json", headers=headers)
            assert head.headers["content-length"] == str(len(response.content))
            ranged = client.get(base + "meta/info.json", headers={**headers, "Range": "bytes=0-3"})
            assert ranged.status_code == 206 and ranged.content == response.content[:4]
            assert client.get(base + "srm_progress.parquet", headers=headers).content == b"PAR1example"
            assert client.get(base + "other.parquet", headers=headers).status_code == 403
    finally:
        bridge.stop()


@pytest.mark.parametrize("header", ["bytes=-0", "bytes=5-1", "bytes=1-2,3-4", "bytes=-", "bytes=20-"])
def test_invalid_ranges(header):
    with pytest.raises(ValueError):
        _byte_range(header, 10)


def test_viewer_unbuilt_status_and_proxy(setup):
    assert setup.client.get("/viewer").status_code == 503
    assert setup.client.get("/api/viewer/status").json()["ready"] is False
    asyncio.run(setup.service.start())
    assert "not built" in setup.service.error
    asyncio.run(setup.service.stop())


def test_proxy_preserves_range_query_and_duplicate_cookies(setup):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(206, headers=[("content-range", "bytes 0-3/10"), ("set-cookie", "a=1"),
                                           ("set-cookie", "b=2"), ("content-type", "text/plain")],
                              stream=httpx.ByteStream(b"data"))
    setup.service.client = httpx.AsyncClient(base_url="http://127.0.0.1:3010", transport=httpx.MockTransport(handler))
    setup.service.ready = True
    response = setup.client.get("/viewer/local/v21/0?_rsc=abc", headers={"Range": "bytes=0-3", "X-DWP-Internal-Token": "attack"})
    assert response.status_code == 206 and response.content == b"data"
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert seen[0].url.path == "/viewer/local/v21/0" and seen[0].url.query == b"_rsc=abc"
    assert seen[0].headers["range"] == "bytes=0-3"
    assert "x-dwp-internal-token" not in seen[0].headers
    asyncio.run(setup.service.stop())


@pytest.mark.skipif(sys.platform != "linux", reason="Hub upload first performs a Linux-only atomic local export")
def test_hub_requires_explicit_new_target(setup, monkeypatch):
    import huggingface_hub
    calls = []
    class FakeApi:
        def __init__(self, **kwargs):
            calls.append("init")
        def create_repo(self, name, **kwargs):
            assert kwargs["exist_ok"] is False
            calls.append("create:" + name)
        def upload_folder(self, **kwargs):
            calls.append("upload:" + kwargs["repo_id"])
    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    payload = {"repo_id": "local/v21", "hf_token": "dummy"}
    assert setup.client.post("/api/push_to_hub", json=payload).status_code == 400
    assert calls == []
    payload.update(push_in_place=False, new_repo_id="local/nope")
    assert setup.client.post("/api/push_to_hub", json=payload).status_code == 400
    assert calls == []
    payload["new_repo_id"] = "owner/new-dataset"
    response = setup.client.post("/api/push_to_hub", json=payload)
    assert response.status_code == 200, response.text
    assert calls == ["init", "create:owner/new-dataset", "upload:owner/new-dataset"]


def test_overlay_is_private_complete_and_repeatable(setup):
    spec = importlib.util.spec_from_file_location("platform_setup_viewer", PROJECT / "setup_viewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_paths = ["src/utils/versionUtils.ts", "src/components/action-insights-panel.tsx",
                      "src/app/[org]/[dataset]/[episode]/fetch-data.ts"]
    originals = {name: (UPSTREAM / name).read_bytes() for name in original_paths}
    source = module.prepare(setup.config, install=False, build=False)
    assert all((UPSTREAM / name).read_bytes() == original for name, original in originals.items())
    assert 'basePath: "/viewer"' in (source / "next.config.ts").read_text()
    assert 'repoId.startsWith("local/") ? "/datasets"' in (source / "src/utils/versionUtils.ts").read_text()
    assert "platformRequestInit(url" in (source / "src/utils/parquetUtils.ts").read_text()
    assert "/viewer/api/auth/" in (source / "src/context/auth-context.tsx").read_text()
    assert "backendOrigin())" in (source / "src/utils/annotationsClient.ts").read_text()
    assert (source / "src/components/annotations-panel.tsx").is_file()
    assert (source / "src/app/api/proxy/[...path]/route.ts").is_file()
    assert "NEXT_PUBLIC_DWP" not in (source / "src/utils/platform-fetch.ts").read_text()
    assert "_dwp_optional_files?: string[]" in (source / "src/utils/versionUtils.ts").read_text()
    episode_source = (source / "src/app/[org]/[dataset]/[episode]/fetch-data.ts").read_text()
    assert "platformProgressCandidates(repoId, PROGRESS_PARQUET_CANDIDATES, availableProgressFiles)" in episode_source
    assert "loadEpisodeProgressGroup(repoId, version, episodeId, rawInfo._dwp_optional_files)" in episode_source
    assert 'if (!Array.isArray(available)) return [];' in (source / "src/utils/platform-progress.ts").read_text()
    assert "_dwp_episode_shards?: string[]" in (source / "src/utils/versionUtils.ts").read_text()
    assert "platformEpisodeMetadataPaths(repoId, info._dwp_episode_shards)" in episode_source
    assert "const actionEntry = platformActionFeature(info.features);" in episode_source
    assert "const path = buildV3EpisodesMetadataPath(chunkIndex, fileIndex);" in episode_source  # Hub retained
    assert "for await (const rows of iterateEpisodeMetadataFilesV3(repoId, version))" in episode_source
    action_source = (source / "src/components/action-insights-panel.tsx").read_text()
    assert action_source.count("platformIsActionColumn(k, CHART_CONFIG.SERIES_NAME_DELIMITER)") == 2
    assert (source / "src/utils/platform-actions.ts").is_file()
    assert (source / "src/utils/platform-episodes.ts").is_file()
    before = (source / "src/utils/platform-fetch.ts").stat().st_mtime_ns
    assert module.prepare(setup.config, install=False, build=False) == source
    assert (source / "src/utils/platform-fetch.ts").stat().st_mtime_ns == before

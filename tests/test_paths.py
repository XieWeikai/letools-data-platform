import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data_web_platform.datasets import DatasetRegistry, dataset_fingerprint, inspect_dataset
from data_web_platform.paths import browse_directory, validate_output_path, validate_path


def make_dataset(root: Path, version="v2.1", length=20):
    """Small ordinary-file datasets, with no network or robot dependencies."""
    meta = root / "meta"
    meta.mkdir(parents=True)
    features = {"observation.state": {"dtype": "float32", "shape": [2]},
                "action": {"dtype": "float32", "shape": [2]},
                "timestamp": {"dtype": "float32", "shape": [1]},
                **{key: {"dtype": "int64", "shape": [1]}
                   for key in ("index", "frame_index", "episode_index", "task_index")}}
    info = {"codebase_version": version, "robot_type": "test", "total_episodes": 1,
            "total_frames": length, "total_tasks": 1, "total_videos": 0,
            "total_chunks": 1, "chunks_size": 1000, "fps": 50,
            "features": features, "splits": {"train": "0:1"}, "video_path": None}
    table = pa.table({"observation.state": pa.array([[i / 100, i / 200] for i in range(length)], type=pa.list_(pa.float32())),
                      "action": pa.array([[i / 100, i / 200] for i in range(length)], type=pa.list_(pa.float32())),
                      "timestamp": pa.array([i / 50 for i in range(length)], type=pa.float32()),
                      "index": list(range(length)), "frame_index": list(range(length)),
                      "episode_index": [0] * length, "task_index": [0] * length})
    stats = {key: {"min": [0], "max": [1], "mean": [.5], "std": [.2], "count": [length]}
             for key in features}
    if version == "v3.0":
        info["data_path"] = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
        data = root / "data/chunk-000/file-000.parquet"
        pq.write_table(pa.Table.from_pylist([{"task_index": 0, "task": "test"}]), meta / "tasks.parquet")
        ep = meta / "episodes/chunk-000/file-000.parquet"
        ep.parent.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([{"episode_index": 0, "length": length,
            "tasks": ["test"], "data/chunk_index": 0, "data/file_index": 0,
            "dataset_from_index": 0, "dataset_to_index": length}]), ep)
    else:
        info["data_path"] = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
        data = root / "data/chunk-000/episode_000000.parquet"
        (meta / "tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": "test"}) + "\n")
        (meta / "episodes.jsonl").write_text(json.dumps({"episode_index": 0, "length": length, "tasks": ["test"]}) + "\n")
        (meta / "episodes_stats.jsonl").write_text(json.dumps({"episode_index": 0, "stats": stats}) + "\n")
    (meta / "info.json").write_text(json.dumps(info))
    (meta / "stats.json").write_text(json.dumps(stats))
    data.parent.mkdir(parents=True)
    pq.write_table(table, data)
    return root


def test_path_canonical_jail(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    assert validate_path(allowed / ".", [allowed]) == allowed
    for path in (outside, allowed / "escape", "relative", "", str(allowed) + "\n"):
        with pytest.raises(ValueError):
            validate_path(path, [allowed])
    with pytest.raises(ValueError):
        validate_path(allowed / "escape/new", [allowed], must_exist=False)
    with pytest.raises(ValueError):
        validate_path(allowed / "missing", [allowed])


def test_output_is_exclusive_disjoint(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    child = source / "child"
    child.mkdir()
    assert validate_output_path(tmp_path / "new", [tmp_path], [source]) == tmp_path / "new"
    for path in (source, source / "new", tmp_path):
        with pytest.raises(ValueError):
            validate_output_path(path, [tmp_path], [source])
    (tmp_path / "dangling").symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="已经存在"):
        validate_output_path(tmp_path / "dangling", [tmp_path], [source])


def test_browser_is_shallow_directory_only(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "raw/data").mkdir(parents=True)
    (allowed / "dataset/meta").mkdir(parents=True)
    (allowed / "dataset/meta/info.json").write_text("{}")
    (allowed / ".hidden").mkdir()
    (allowed / "file.txt").write_text("not a directory")
    (allowed / "escape").symlink_to(tmp_path, target_is_directory=True)
    result = browse_directory(allowed, [allowed])
    assert result["parent"] is None
    assert result["directories"] == result["dirs"]
    assert {entry["name"] for entry in result["dirs"]} == {"raw", "dataset"}
    assert next(e for e in result["dirs"] if e["name"] == "raw")["is_recording"]
    assert next(e for e in result["dirs"] if e["name"] == "dataset")["is_dataset"]
    with pytest.raises(ValueError):
        browse_directory(allowed / "file.txt", [allowed])


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_registry_roundtrip_versions_and_fresh_info(tmp_path, version):
    source = make_dataset(tmp_path / "Original dataset", version)
    before = dataset_fingerprint(source)
    config = SimpleNamespace(allowed_roots=[tmp_path], state_dir=tmp_path / "state")
    registry = DatasetRegistry(config)
    record = registry.register(source)
    assert record["repo_id"].startswith("local/original-dataset-")
    assert registry.get(record["id"]) == registry.get(record["repo_id"])
    assert DatasetRegistry(config).all() == [record]
    assert dataset_fingerprint(source) == before
    info_path = source / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["robot_type"] = "changed"
    info_path.write_text(json.dumps(info))
    assert registry.get(record["id"])["info"]["robot_type"] == "changed"
    registry.register(source)
    assert len(registry.all()) == 1


@pytest.mark.parametrize("breakage", ["unsupported", "counts", "parquet", "external-link", "escape-template"])
def test_incomplete_and_escaping_dataset_rejected(tmp_path, breakage):
    source = make_dataset(tmp_path / "source")
    info_path = source / "meta/info.json"
    info = json.loads(info_path.read_text())
    if breakage == "unsupported":
        info["codebase_version"] = "v9.0"
    elif breakage == "counts":
        info["total_frames"] = 999
    elif breakage == "escape-template":
        info["data_path"] = str(tmp_path / "external.parquet")
        (tmp_path / "external.parquet").write_bytes(b"x")
    elif breakage == "external-link":
        (source / "leak").symlink_to(tmp_path, target_is_directory=True)
    elif breakage == "parquet":
        (source / "data/chunk-000/episode_000000.parquet").unlink()
    info_path.write_text(json.dumps(info))
    with pytest.raises(ValueError):
        inspect_dataset(source, [tmp_path])


def test_fingerprint_invalidates_replaced_added_removed_files(tmp_path):
    source = make_dataset(tmp_path / "source")
    first = dataset_fingerprint(source)
    (source / "notes.txt").write_text("new")
    second = dataset_fingerprint(source)
    assert first != second
    (source / "notes.txt").unlink()
    assert dataset_fingerprint(source) != second


def test_registered_media_lookup_does_not_rescan_all_parquet(tmp_path, monkeypatch):
    source = make_dataset(tmp_path / "source")
    registry = DatasetRegistry(SimpleNamespace(allowed_roots=[tmp_path], state_dir=tmp_path / "state"))
    record = registry.register(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("full scan on media lookup")

    monkeypatch.setattr("data_web_platform.datasets.inspect_dataset", forbidden)
    assert registry.get(record["id"])["path"] == str(source)
    assert registry.all() == [record]
    with pytest.raises(AssertionError, match="full scan"):
        registry.get(record["id"], deep=True)
    (source / ".dwp-incomplete").touch()
    with pytest.raises(ValueError, match="尚未完成"):
        registry.get(record["id"])

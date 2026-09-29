import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from data_web_platform.datasets import DatasetRegistry, dataset_fingerprint
from data_web_platform.doctor import (CHECKS, DoctorReports, DoctorRequest,
    build_doctor_command, create_doctor_router, prepare_doctor_request, run_check)
from data_web_platform.doctor_worker import run_prepared
from test_paths import make_dataset


@pytest.fixture
def context(tmp_path):
    config = SimpleNamespace(allowed_roots=[tmp_path], state_dir=tmp_path / "state", python=Path("/isolated/.venv/bin/python"))
    source = make_dataset(tmp_path / "source;harmless")
    registry = DatasetRegistry(config)
    record = registry.register(source)
    return config, registry, record, source


@pytest.mark.parametrize("payload", [
    {"operation": "shell"}, {"checks": ["--exec"]}, {"checks": []},
    {"fixes": ["delete"]}, {"command": "rm"}, {"threshold": float("inf")},
    {"operation": "fix", "confirm": "true", "output": "/new"},
    {"operation": "trim", "confirm": False, "output": "/new"},
    {"operation": "fix", "confirm": True},
    {"operation": "merge-check", "datasets": ["local/a"]},
    {"operation": "merge-check", "datasets": ["local/a", "local/a"]},
])
def test_only_structured_allowlisted_requests(payload):
    with pytest.raises(ValidationError):
        DoctorRequest.model_validate({"dataset": "local/test", **payload})


def test_command_is_argv_not_shell(context):
    config, registry, record, source = context
    argv = build_doctor_command({"dataset": record["repo_id"]}, registry, config)
    assert argv[:4] == [str(config.python), "-m", "data_web_platform.doctor_worker", "--request-json"]
    prepared = json.loads(argv[4])
    assert prepared["sources"] == [str(source)]
    assert prepared["output"] is None
    assert prepared["options"]["checks"] is None


def test_prepare_mutation_no_overwrite_or_nested_output(context):
    config, registry, record, source = context
    payload = {"operation": "fix", "dataset": record["id"], "confirm": True}
    for output in (source, source / "nested", source.parent):
        with pytest.raises(ValueError):
            prepare_doctor_request({**payload, "output": str(output)}, registry, config)
    prepared = prepare_doctor_request({**payload, "output": str(source.parent / "new")}, registry, config)
    assert prepared["destination"] == prepared["output"]


def test_report_cache_invalidates(context, monkeypatch):
    _, registry, record, source = context
    calls = []
    def check(path, **kwargs):
        calls.append(path)
        return {"overall_severity": "PASS", "results": []}
    monkeypatch.setattr("data_web_platform.doctor.run_check", check)
    reports = DoctorReports(registry)
    assert not reports.get(record["id"])["cached"]
    assert reports.get(record["repo_id"])["cached"]
    (source / "changed.txt").write_text("change")
    assert not reports.get(record["id"])["cached"]
    reports.get(record["id"], refresh=True)
    assert len(calls) == 3


def test_router_all_checks_async_jobs_and_escaped_page(context, monkeypatch):
    config, registry, record, _ = context
    requests = []
    jobs = SimpleNamespace(submit_doctor=lambda request: requests.append(request) or {"id": "job1", "status": "queued"})
    app = FastAPI()
    app.include_router(create_doctor_router(config, registry, jobs))
    client = TestClient(app)
    assert client.get("/api/doctor/options").json()["checks"] == list(CHECKS)
    assert len(CHECKS) == 12
    html = client.get("/doctor", params={"dataset": '\"><script>alert(1)</script>'}).text
    assert '&lt;script&gt;' in html and '<script>alert(1)' not in html
    result = client.post("/api/doctor/jobs", json={"dataset": record["repo_id"], "operation": "score"})
    assert result.status_code == 202 and requests[0]["operation"] == "score"
    assert client.post("/api/doctor/jobs", json={"dataset": "missing"}).status_code == 404
    assert client.post("/api/doctor/jobs", json={"dataset": record["id"], "shell": "x"}).status_code == 422
    monkeypatch.setattr("data_web_platform.doctor.run_check", lambda *a, **k: {"overall_severity": "PASS"})
    assert client.get("/doctor/report.json", params={"dataset": record["id"]}).status_code == 200


def test_real_all_12_readonly_checks(context):
    _, _, _, source = context
    before = dataset_fingerprint(source)
    report = run_check(source)
    assert len(report["checks"]) == 12
    assert report["overall_severity"] in {"PASS", "WARN", "FAIL"}
    assert dataset_fingerprint(source) == before


@pytest.mark.parametrize("operation", ["check", "score", "gate", "merge-check"])
def test_real_readonly_worker_operations(context, operation, capsys):
    config, registry, record, source = context
    payload = {"operation": operation, "dataset": record["id"]}
    if operation == "merge-check":
        second = registry.register(make_dataset(source.parent / "second"))
        payload["datasets"] = [record["id"], second["id"]]
    before = dataset_fingerprint(source)
    code = run_prepared(prepare_doctor_request(payload, registry, config))
    assert code in (0, 1)  # A miniature fixture may fail a training gate.
    events = [json.loads(line.removeprefix("DWP_EVENT ")) for line in capsys.readouterr().out.splitlines() if line.startswith("DWP_EVENT ")]
    assert any(event["event"] == "result" for event in events)
    assert dataset_fingerprint(source) == before


def test_fix_only_new_independent_copy_and_no_sibling_backup(context, monkeypatch, capsys):
    from lerobot_doctor.fix import FixResult
    config, registry, record, source = context
    destination = source.parent / "fixed"
    before = dataset_fingerprint(source)
    def fix(path, **kwargs):
        assert path == destination and kwargs["backup"] is False
        assert (path / ".dwp-incomplete").exists()
        assert (path / "meta/info.json").stat().st_ino != (source / "meta/info.json").stat().st_ino
        (path / "review.txt").write_text("only the copy")
        return FixResult(fixed=["test"], skipped=[], errors=[])
    monkeypatch.setattr("lerobot_doctor.fix.fix_dataset", fix)
    request = prepare_doctor_request({"operation": "fix", "dataset": record["id"],
        "confirm": True, "output": str(destination)}, registry, config)
    assert run_prepared(request) == 0
    assert (destination / "review.txt").exists()
    assert not (source / "review.txt").exists()
    assert not (destination.parent / "fixed_backup").exists()
    assert not (destination / ".dwp-incomplete").exists()
    assert registry.register(destination)["path"] == str(destination)
    assert dataset_fingerprint(source) == before
    with pytest.raises(ValueError, match="已经存在"):
        run_prepared(request)


@pytest.mark.parametrize("failure", ["copy", "exception", "report", "validation", "source_changed"])
def test_failed_fix_copy_cannot_be_registered(context, monkeypatch, failure):
    from lerobot_doctor.fix import FixResult
    config, registry, record, source = context
    destination = source.parent / "failed-fix"
    before = dataset_fingerprint(source)

    def fix(path, **kwargs):
        assert (path / ".dwp-incomplete").exists()
        if failure == "exception":
            raise RuntimeError("simulated upstream fix failure")
        return FixResult(fixed=[], skipped=[], errors=["simulated failure"] if failure == "report" else [])

    monkeypatch.setattr("lerobot_doctor.fix.fix_dataset", fix)
    if failure == "copy":
        def copy(*args, **kwargs):
            raise OSError("simulated partial copy failure")
        monkeypatch.setattr("data_web_platform.doctor_worker.shutil.copytree", copy)
    if failure == "validation":
        from dataclasses import make_dataclass
        Validation = make_dataclass("Validation", [("valid", bool)])
        monkeypatch.setattr("letools.validation.validate_dataset", lambda *a, **k: Validation(False))
    if failure == "source_changed":
        calls = iter([before, before, before, "changed"])
        monkeypatch.setattr("data_web_platform.doctor_worker.dataset_fingerprint", lambda _: next(calls))
    request = prepare_doctor_request({"operation": "fix", "dataset": record["id"],
        "confirm": True, "output": str(destination)}, registry, config)
    if failure in {"copy", "exception", "source_changed"}:
        with pytest.raises((OSError, RuntimeError, ValueError)):
            run_prepared(request)
    else:
        assert run_prepared(request) == 1
    assert (destination / ".dwp-incomplete").exists()
    with pytest.raises(ValueError, match="dwp-incomplete"):
        registry.register(destination)
    assert dataset_fingerprint(source) == before


def test_mutation_rechecks_confirmation_and_links(context):
    config, registry, record, source = context
    request = prepare_doctor_request({"operation": "trim", "dataset": record["id"],
        "confirm": True, "dry_run": True, "output": str(source.parent / "trimmed")}, registry, config)
    with pytest.raises(ValidationError):
        run_prepared({**request, "confirm": False})
    (source / "linked-info").symlink_to(source / "meta/info.json")
    with pytest.raises(ValueError, match="符号链接"):
        run_prepared(request)
    assert not Path(request["output"]).exists()


@pytest.mark.parametrize("failure", ["validation", "source_changed"])
def test_trim_worker_retains_marker_until_outer_validation(context, monkeypatch, failure):
    # Import first: the native adapter keeps its real validator while we simulate
    # an adverse result in the worker's second/final validation pass.
    import data_web_platform.trim
    config, registry, record, source = context
    destination = source.parent / "failed-trim"
    before = dataset_fingerprint(source)
    if failure == "validation":
        from dataclasses import make_dataclass
        Validation = make_dataclass("Validation", [("valid", bool)])
        monkeypatch.setattr("letools.validation.validate_dataset", lambda *a, **k: Validation(False))
    else:
        calls = iter([before, "changed"])
        monkeypatch.setattr("data_web_platform.doctor_worker.dataset_fingerprint", lambda _: next(calls))
    request = prepare_doctor_request({"operation": "trim", "dataset": record["id"],
        "confirm": True, "output": str(destination), "threshold": .001, "min_active": 1}, registry, config)
    if failure == "source_changed":
        with pytest.raises(ValueError, match="源数据发生变化"):
            run_prepared(request)
    else:
        assert run_prepared(request) == 1
    assert (destination / ".dwp-incomplete").exists()
    with pytest.raises(ValueError, match="dwp-incomplete"):
        registry.register(destination)
    assert dataset_fingerprint(source) == before


def test_trim_dry_run_is_nonmutating(context, capsys):
    config, registry, record, source = context
    before = dataset_fingerprint(source)
    request = prepare_doctor_request({"operation": "trim", "dataset": record["id"],
        "confirm": True, "dry_run": True, "threshold": .001,
        "output": str(source.parent / "trimmed")}, registry, config)
    assert request["destination"] is None  # JobManager must not auto-register a preview.
    assert run_prepared(request) == 0
    events = [json.loads(line[10:]) for line in capsys.readouterr().out.splitlines() if line.startswith("DWP_EVENT ")]
    result = next(event["result"] for event in events if event["event"] == "result")
    assert result["preview_only"] is True
    assert result["report"]["implementation"] == "synchronized-letools"
    assert not Path(request["output"]).exists()
    assert dataset_fingerprint(source) == before


def _video_dataset(root, version, *, gap=False):
    """Three episodes, two cameras, B-frames; v3 cameras share one video shard.

    Episode 0 is static; episode 1 moves state despite a constant command;
    episode 2 changes observation.action despite a constant state. Video pixels
    encode the original global frame number, independently of output metadata.
    """
    from fractions import Fraction
    import av
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    root.mkdir()
    meta = root / "meta"
    meta.mkdir()
    fps, length = 10, 24
    keys = ["observation.images.head", "observation.images.wrist"]
    features = {"observation.state": {"dtype": "float32", "shape": [2]},
                "observation.action": {"dtype": "float32", "shape": [2]},
                "timestamp": {"dtype": "float32", "shape": [1]},
                **{key: {"dtype": "int64", "shape": [1]}
                   for key in ("index", "frame_index", "episode_index", "task_index")},
                **{key: {"dtype": "video", "shape": [48, 64, 3],
                    "names": ["height", "width", "channels"],
                    "info": {"video.fps": fps, "video.codec": "h264", "video.pix_fmt": "yuv420p"}}
                   for key in keys}}
    info = {"codebase_version": version, "robot_type": "test", "total_episodes": 3,
            "total_frames": 72, "total_tasks": 1, "fps": fps, "features": features,
            "total_chunks": 1, "chunks_size": 1000, "total_videos": 6,
            "splits": {"train": "0:2", "val": "2:3"}}
    tables, episodes, stats = [], [], []
    for index in range(3):
        motion = np.clip(np.arange(length) - (5 if index == 1 else 3), 0, 10 if index == 1 else 8) * .1
        state = motion if index == 1 else np.zeros(length)
        action = motion if index == 2 else np.zeros(length)
        table = pa.table({"observation.state": pa.array([[value, 0] for value in state], type=pa.list_(pa.float32())),
                          "observation.action": pa.array([[0, value] for value in action], type=pa.list_(pa.float32())),
                          "timestamp": pa.array(np.arange(length) / fps, type=pa.float32()),
                          "index": np.arange(index * length, (index + 1) * length),
                          "frame_index": np.arange(length), "episode_index": [index] * length,
                          "task_index": [0] * length})
        tables.append(table)
        episodes.append({"episode_index": index, "length": length, "tasks": ["test"]})
        stats.append({"episode_index": index, "stats": {}})
    if version == "v2.1":
        info.update(data_path="data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
                    video_path="videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4")
        for index, table in enumerate(tables):
            path = root / info["data_path"].format(episode_chunk=0, episode_index=index)
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, path)
        for name, rows in (("tasks", [{"task_index": 0, "task": "test"}]), ("episodes", episodes), ("episodes_stats", stats)):
            (meta / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    else:
        info.update(data_path="data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                    video_path="videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4")
        data = root / "data/chunk-000/file-000.parquet"
        data.parent.mkdir(parents=True)
        pq.write_table(pa.concat_tables(tables), data)
        pq.write_table(pa.Table.from_pylist([{"task_index": 0, "task": "test"}]), meta / "tasks.parquet")
        for index, row in enumerate(episodes):
            row.update({"data/chunk_index": 0, "data/file_index": 0,
                        "dataset_from_index": index * length, "dataset_to_index": (index + 1) * length})
            for key in keys:
                row.update({f"videos/{key}/chunk_index": 0, f"videos/{key}/file_index": 0,
                            f"videos/{key}/from_timestamp": index * length / fps,
                            f"videos/{key}/to_timestamp": (index + 1) * length / fps})
        ep = meta / "episodes/chunk-000/file-000.parquet"
        ep.parent.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(episodes), ep)
    for camera, key in enumerate(keys):
        groups = [(index, range(index * length, (index + 1) * length)) for index in range(3)] if version == "v2.1" else [(0, range(72))]
        for index, indices in groups:
            path = root / info["video_path"].format(video_key=key, episode_chunk=0, episode_index=index,
                                                  chunk_index=0, file_index=0)
            path.parent.mkdir(parents=True, exist_ok=True)
            with av.open(str(path), "w") as container:
                stream = container.add_stream("libx264", rate=fps, options={"crf": "10", "preset": "medium"})
                stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
                stream.codec_context.max_b_frames = 2
                stream.codec_context.gop_size = 100
                stream.codec_context.thread_count = 1
                for local, global_frame in enumerate(indices):
                    if gap and camera == 0 and global_frame == 33:
                        continue
                    image = np.full((48, 64, 3), 10 + global_frame * 3 + camera * 4, np.uint8)
                    frame = av.VideoFrame.from_ndarray(image, format="rgb24")
                    frame.pts, frame.time_base = local, Fraction(1, fps)
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
    (meta / "info.json").write_text(json.dumps(info))
    (meta / "stats.json").write_text("{}")
    return root


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_synchronized_trim_preserves_frame_identity_and_source(tmp_path, version):
    import av
    import numpy as np
    from letools.plugins import open_dataset
    from letools.validation import validate_dataset
    from data_web_platform.datasets import inspect_dataset
    from data_web_platform.trim import trim_dataset

    source = _video_dataset(tmp_path / "source", version)
    original = open_dataset(source)
    with av.open(str(original.episodes[1].videos[original.metadata.video_keys[0]].path)) as container:
        assert container.streams.video[0].codec_context.has_b_frames
    before = dataset_fingerprint(source)
    destination = tmp_path / "trimmed"
    report = trim_dataset(source, destination, [tmp_path], threshold=.05, min_active=5)
    assert (report["source_frames"], report["kept_frames"], report["dropped_episodes"]) == (72, 20, 1)
    assert report["details"][1]["start_frame"] == 5
    assert report["details"][2]["start_frame"] == 3
    assert dataset_fingerprint(source) == before
    assert not (destination / ".dwp-incomplete").exists()
    assert not list(destination.glob(".trim-work-*"))
    assert inspect_dataset(destination, [tmp_path])[1]["codebase_version"] == version
    assert validate_dataset(destination, deep=True).valid
    result = open_dataset(destination)
    assert result.metadata.splits == {"train": "0:1", "val": "1:2"}
    expected_ranges = [range(29, 40), range(51, 60)]
    offset = 0
    decoded = {}
    for episode, expected_frames in zip(result.episodes, expected_ranges):
        table = result.read_episode(episode)
        np.testing.assert_array_equal(table["frame_index"], np.arange(episode.length))
        np.testing.assert_array_equal(table["index"], np.arange(offset, offset + episode.length))
        np.testing.assert_array_equal(table["episode_index"], np.full(episode.length, episode.index))
        np.testing.assert_allclose(table["timestamp"], np.arange(episode.length) / 10)
        offset += episode.length
        for camera, key in enumerate(result.metadata.video_keys):
            video = episode.videos[key]
            if video.path not in decoded:
                with av.open(str(video.path)) as container:
                    stream = container.streams.video[0]
                    assert (stream.width, stream.height, stream.codec_context.name) == (64, 48, "h264")
                    frames = list(container.decode(stream))
                    decoded[video.path] = [float(frame.to_ndarray(format="rgb24").mean()) for frame in frames]
                    np.testing.assert_allclose([float(frame.pts * frame.time_base) for frame in frames], np.arange(len(frames)) / 10)
            first = round(video.start * 10)
            actual = decoded[video.path][first:first + episode.length]
            np.testing.assert_allclose(actual, [10 + index * 3 + camera * 4 for index in expected_frames], atol=4)
            assert episode.stats[key]["count"] == [episode.length]
            assert np.asarray(episode.stats[key]["mean"]).shape == (3, 1, 1)
        assert episode.stats["frame_index"]["max"] == [episode.length - 1]
    assert json.loads((destination / "meta/stats.json").read_text())["index"]["count"] == [20]
    # Backend files are new independent files, not links back into production.
    source_inodes = {path.stat().st_ino for path in source.rglob("*") if path.is_file()}
    assert not any(path.stat().st_ino in source_inodes for path in destination.rglob("*") if path.is_file())
    with pytest.raises(ValueError, match="已经存在"):
        trim_dataset(source, destination, [tmp_path], threshold=.05, min_active=5)


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_synchronized_trim_rejects_real_video_gap(tmp_path, version):
    from data_web_platform.trim import trim_dataset
    from data_web_platform.datasets import inspect_dataset
    source = _video_dataset(tmp_path / "source", version, gap=True)
    before = dataset_fingerprint(source)
    destination = tmp_path / "failed"
    with pytest.raises(ValueError, match="缺帧"):
        trim_dataset(source, destination, [tmp_path], threshold=.05, min_active=5)
    assert dataset_fingerprint(source) == before
    assert (destination / ".dwp-incomplete").exists()
    with pytest.raises(ValueError, match="尚未完成"):
        inspect_dataset(destination, [tmp_path])


def test_synchronized_trim_all_static_nan_time_gap_and_dry_run(tmp_path):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from data_web_platform.trim import trim_dataset
    source = make_dataset(tmp_path / "source")
    path = source / "data/chunk-000/episode_000000.parquet"
    table = pq.read_table(path)
    for key in ("observation.state", "action"):
        table = table.set_column(table.column_names.index(key), key, pa.array([[0., 0.]] * len(table), type=pa.list_(pa.float32())))
    pq.write_table(table, path)
    with pytest.raises(ValueError, match="所有轨迹"):
        trim_dataset(source, tmp_path / "static", [tmp_path])
    assert not (tmp_path / "static").exists()
    before = dataset_fingerprint(source)
    report = trim_dataset(source, tmp_path / "preview", [tmp_path], dry_run=True, keep_static=True)
    assert report["kept_frames"] == 20 and not (tmp_path / "preview").exists()
    trim_dataset(source, tmp_path / "kept", [tmp_path], keep_static=True)
    assert dataset_fingerprint(source) == before
    changed = table.set_column(table.column_names.index("action"), "action", pa.array([[float("nan"), 0.]] * len(table), type=pa.list_(pa.float32())))
    pq.write_table(changed, path)
    with pytest.raises(ValueError, match="NaN"):
        trim_dataset(source, tmp_path / "nan", [tmp_path], keep_static=True)
    times = np.arange(len(table)) / 50
    times[10:] += 1
    changed = table.set_column(table.column_names.index("timestamp"), "timestamp", pa.array(times, type=pa.float32()))
    pq.write_table(changed, path)
    with pytest.raises(ValueError, match="时间轴"):
        trim_dataset(source, tmp_path / "gap", [tmp_path], keep_static=True)
    assert not (tmp_path / "nan").exists() and not (tmp_path / "gap").exists()


def test_doctor_worker_native_trim_actual_output(context, capsys):
    config, registry, record, source = context
    destination = source.parent / "trimmed"
    request = prepare_doctor_request({"operation": "trim", "dataset": record["id"],
        "confirm": True, "threshold": .001, "output": str(destination)}, registry, config)
    assert run_prepared(request) == 0
    events = [json.loads(line[10:]) for line in capsys.readouterr().out.splitlines() if line.startswith("DWP_EVENT ")]
    result = next(event["result"] for event in events if event["event"] == "result")
    assert result["passed"] and result["validation"]["valid"] and not result["preview_only"]
    assert registry.register(destination)["path"] == str(destination)

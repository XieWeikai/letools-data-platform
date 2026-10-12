"""Worker orchestration contracts; provider/codec integration is tested separately.

Runtime services are injected because the platform may use an external LeTools
environment. These tests never probe hardware, encode videos or alter raw data.
"""

from dataclasses import dataclass, field
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from data_web_platform import worker


@dataclass
class Config:
    task: str
    fps: int = 50
    wrench_fps: int = 200


@dataclass
class BackendConfig:
    workers: int
    video_workers: int


@dataclass
class ConversionResult:
    output: str
    total_frames: int = 12


@dataclass
class ValidationResult:
    valid: bool = True
    errors: list = field(default_factory=list)


def raw_snapshot(roots):
    return {str(path): path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file()}


def _module(monkeypatch, name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    roots = []
    for index in range(2):
        root = tmp_path / f"my_bag-{index}"
        root.mkdir()
        (root / "session.json").write_text(json.dumps({"phase": "saved"}))
        (root / "recording.mcap").write_bytes(b"immutable original recording fixture")
        (root / "data").mkdir()
        (root / "data/metadata.yaml").write_text(
            f"rosbag2_bagfile_information:\n  duration:\n    nanoseconds: {(index + 1) * 1_000_000_000}\n")
        roots.append(root)
    job = tmp_path / "job"
    job.mkdir()
    destination = tmp_path / "output"
    request = {"kind": "convert", "recordings": list(map(str, roots)),
               "params": {"task": "Pick and place", "fps": 50, "wrench_fps": 200},
               "destination": str(destination), "target_version": "v2.1", "workers": 2, "video_workers": 4}
    report = {"format": "tianji-diagnostics-v1", "recordings": 2, "valid": True,
              "valid_recordings": 2, "invalid_recordings": 0, "valid_frames": 12,
              "results": [{"bag": root.name, "valid": True, "frames": 6,
                           "issues": [], "completed_checks": ["complete_video_decode_frame_mapping_and_dimensions"]}
                          for root in roots]}
    state = SimpleNamespace(
        roots=roots, job=job, destination=destination, request=request,
        request_file=job / "request.json", report=report,
        fused_result={"report": report, "manifest": {"episodes": [{"index": 0, "length": 6}, {"index": 1, "length": 6}],
                        "source_selection": {"mode": "explicit_valid_only", "episode_mapping": [
                            {"source_recording_index": i, "episode_index": i} for i in range(2)]},
                        "timings": {"checked_prepare_seconds": 2.0}}},
        fused_error=None, during_fused=None, convert_error=None,
        validation=ValidationResult(), validation_error=None, during_convert=None, output_override={},
        calls=[], events=[], fused_kwargs=None, configs=[], source=object())

    def save():
        state.request_file.write_text(json.dumps(state.request))
        return state.request_file

    state.save = save
    save()
    monkeypatch.setattr(worker.os, "nice", lambda _: None)
    monkeypatch.setattr(worker, "emit", lambda event, **values: state.events.append({"event": event, **values}))

    def independent_check(selected, config, *, deep_video, progress, workers=None):
        state.calls.append("check")
        state.configs.append(config)
        assert selected == job / "selected" and deep_video is state.request.get("deep_video", True)
        state.check_workers = workers
        assert sorted(path.resolve() for path in selected.iterdir()) == sorted(roots)
        for index, item in enumerate(state.report["results"], 1):
            progress(index, len(roots), item)
        return state.report

    def checked_prepare(selected, cache, config, **kwargs):
        state.calls.append("check_prepare")
        state.configs.append(config)
        state.fused_kwargs = kwargs
        assert selected == job / "selected" and cache == job / "cache"
        if state.fused_error is not None:
            raise state.fused_error
        if state.during_fused is not None:
            state.during_fused()
        for index, item in enumerate(state.report["results"], 1):
            kwargs["progress"](index, len(roots), item)
        return state.fused_result

    def open_cache(path, config):
        state.calls.append("open_cache")
        assert path == job / "cache" and config is state.configs[-1]
        return state.source

    def new_staging(target):
        state.calls.append("staging")
        assert target == destination
        return job / "staging"

    def convert(source, target, version, *, config):
        state.calls.append("convert")
        assert source is state.source and version == state.request["target_version"]
        assert (config.workers, config.video_workers) == (state.request["workers"], state.request["video_workers"])
        if state.convert_error:
            raise state.convert_error
        target.mkdir()
        (target / "meta").mkdir()
        (target / "meta/info.json").write_text(json.dumps({"total_episodes": state.report["valid_recordings"],
            "total_frames": state.report["valid_frames"], "fps": state.request["params"]["fps"], **state.output_override}))
        if state.during_convert:
            state.during_convert()
        return ConversionResult(str(target), state.report["valid_frames"])

    def validate(target, *, deep):
        state.calls.append("validate")
        assert target == job / "staging" and deep is True
        if state.validation_error:
            raise state.validation_error
        return state.validation

    def publish(staging, target):
        state.calls.append("publish")
        assert state.validation.valid and target == destination
        staging.rename(target)

    # Import targets used by worker.run, without requiring the external robot
    # provider/GPU libraries in the platform unit-test environment.
    _module(monkeypatch, "tianji_provider", __path__=[])
    _module(monkeypatch, "tianji_provider.provider", TianjiConfig=Config,
            provider=SimpleNamespace(open=open_cache))
    _module(monkeypatch, "tianji_provider.diagnostics", check_dataset=independent_check)
    _module(monkeypatch, "tianji_provider.fused", check_and_prepare=checked_prepare)
    _module(monkeypatch, "letools", __path__=[])
    _module(monkeypatch, "letools.conversion", convert=convert, ConversionConfig=BackendConfig)
    _module(monkeypatch, "letools.validation", validate_dataset=validate)
    monkeypatch.setattr(worker, "new_staging", new_staging)
    monkeypatch.setattr(worker, "publish_noreplace", publish)
    return state


def running_stages(state):
    return [event["name"] for event in state.events if event["event"] == "stage" and event["status"] == "running"]


def assert_no_conversion(state):
    assert not {"open_cache", "staging", "convert", "validate", "publish"}.intersection(state.calls)
    assert not state.destination.exists()
    assert not any(event["event"] == "result" for event in state.events)


def test_check_only_keeps_independent_read_only_diagnostics(scenario):
    state = scenario
    state.request["kind"] = "check"
    state.fused_error = AssertionError("Check-only must not call preparation")
    before = raw_snapshot(state.roots)
    worker.run(state.save())
    assert state.calls == ["check"]
    assert running_stages(state) == ["check"]
    assert raw_snapshot(state.roots) == before
    assert not (state.job / "cache").exists() and not state.destination.exists()
    assert json.loads((state.job / "check.json").read_text()) == state.report
    assert [event["report"] for event in state.events if event["event"] == "check_report"] == [state.report]


def test_conversion_checks_every_recording_and_selects_valid_only(scenario):
    state = scenario
    state.request["prepare_valid_only"] = False  # Worker owns the Web policy.
    before = raw_snapshot(state.roots)
    worker.run(state.save())
    assert state.fused_kwargs["prepare_valid_only"] is True
    assert state.fused_kwargs["video_encoder"] == "auto"
    assert state.calls == ["check_prepare", "open_cache", "staging", "convert", "validate", "publish"]
    assert running_stages(state) == ["check_prepare", "convert", "validate", "publish"]
    assert state.destination.is_dir() and raw_snapshot(state.roots) == before
    assert json.loads((state.job / "check.json").read_text()) == state.report
    assert [event["item"] for event in state.events if event["event"] == "check_result"] == state.report["results"]
    result, = [event["result"] for event in state.events if event["event"] == "result"]
    assert set(result["timings"]) == {"check_prepare", "convert", "validate", "publish"}
    assert result["destination"] == str(state.destination)
    summary = result["conversion_summary"]
    assert summary["input"]["episodes"] == 2 and summary["input"]["duration_seconds"] == 3
    assert summary["output"] == {"episodes": 2, "frames": 12, "fps": 50, "duration_seconds": .24}
    assert summary["skipped"]["episodes"] == 0


def test_mixed_selection_converts_only_valid_episode_and_reports_skip(scenario):
    state = scenario
    state.report.update(valid=False, valid_recordings=1, invalid_recordings=1, valid_frames=6)
    state.report["results"][0].update(valid=False, frames=0, issues=[{"code": "video_decode_error", "severity": "error"}])
    state.fused_result["manifest"]["episodes"] = [{"index": 0, "length": 6}]
    state.fused_result["manifest"]["source_selection"]["episode_mapping"] = [{"source_recording_index": 1, "episode_index": 0}]
    before = raw_snapshot(state.roots)
    worker.run(state.save())
    assert state.destination.exists()
    assert raw_snapshot(state.roots) == before
    assert json.loads((state.job / "check.json").read_text())["valid"] is False
    result, = [e["result"] for e in state.events if e["event"] == "result"]
    summary = result["conversion_summary"]
    assert summary["accepted"]["duration_seconds"] == 2
    assert summary["skipped"]["episodes"] == 1 and summary["skipped"]["duration_seconds"] == 1
    assert summary["skipped"]["recordings"][0]["path"] == str(state.roots[0])
    assert summary["skipped"]["recordings"][0]["issues"][0]["code"] == "video_decode_error"
    assert summary["output"]["episodes"] == 1 and summary["output"]["duration_seconds"] == .12


@pytest.mark.parametrize("kind", ["convert", "check"])
def test_all_invalid_keeps_report_and_never_publishes(scenario, kind):
    state = scenario
    state.request["kind"] = kind
    state.report.update(valid=False, valid_recordings=0, invalid_recordings=2, valid_frames=0)
    for item in state.report["results"]:
        item.update(valid=False, frames=0, issues=[{"code": "video_decode_error", "severity": "error"}])
    state.fused_result["manifest"] = None
    with pytest.raises(ValueError, match="未通过检查|严格检查未通过"):
        worker.run(state.save())
    assert json.loads((state.job / "check.json").read_text()) == state.report
    assert_no_conversion(state)


@pytest.mark.parametrize("bad", ["mapping", "count", "length"])
def test_manifest_must_match_exact_passing_subset(scenario, bad):
    manifest = scenario.fused_result["manifest"]
    if bad == "mapping":
        manifest["source_selection"]["episode_mapping"][0]["source_recording_index"] = 1
    elif bad == "count":
        manifest["episodes"].pop()
    else:
        manifest["episodes"][0]["length"] = 8
    with pytest.raises(ValueError, match="不一致"):
        worker.run(scenario.request_file)
    assert_no_conversion(scenario)


def test_missing_raw_duration_is_unknown_not_zero(scenario):
    (scenario.roots[0] / "data/metadata.yaml").unlink()
    worker.run(scenario.request_file)
    summary = [e["result"] for e in scenario.events if e["event"] == "result"][0]["conversion_summary"]
    assert summary["input"] == {"episodes": 2, "duration_seconds": None,
                                "known_duration_seconds": 2, "unknown_duration_episodes": 1}


def test_provider_execution_controls_reach_fused_and_check_paths(scenario):
    state = scenario
    state.request.update(provider_workers=3, video_encoder="libx264", deep_video=False)
    state.request["params"]["wrench_fps"] = 400
    worker.run(state.save())
    assert state.fused_kwargs["workers"] == 3
    assert state.fused_kwargs["video_encoder"] == "libx264"
    assert "deep_video" not in state.fused_kwargs  # Fused API always fully decodes.
    assert state.configs[0].wrench_fps == 400


def test_fast_check_uses_requested_workers_and_does_not_prepare(scenario):
    scenario.request.update(kind="check", deep_video=False, provider_workers=2)
    worker.run(scenario.save())
    assert scenario.check_workers == 2 and scenario.calls == ["check"]


def test_source_changes_during_conversion_prevent_publication(scenario):
    scenario.during_convert = lambda: (scenario.roots[0] / "recording.mcap").write_bytes(b"changed")
    with pytest.raises(ValueError, match="转换期间原始数据发生变化"):
        worker.run(scenario.request_file)
    assert "publish" not in scenario.calls and not scenario.destination.exists()


@pytest.mark.parametrize("field", ["total_episodes", "total_frames", "fps"])
def test_backend_cannot_publish_counts_different_from_prepared_subset(scenario, field):
    scenario.output_override[field] = 999
    with pytest.raises(ValueError, match="通过项不一致"):
        worker.run(scenario.request_file)
    assert "publish" not in scenario.calls


def test_omitted_recording_is_rejected_before_conversion(scenario):
    state = scenario
    state.report["recordings"] -= 1
    with pytest.raises(ValueError, match="检查数量与选择数量不同"):
        worker.run(state.request_file)
    assert_no_conversion(state)


@pytest.mark.parametrize("fault", ["missing-report", "null-report", "missing-manifest", "null-manifest", "malformed-manifest"])
def test_missing_or_malformed_fused_artifacts_never_convert(scenario, fault):
    state = scenario
    if fault == "missing-report":
        state.fused_result.pop("report")
    elif fault == "null-report":
        state.fused_result["report"] = None
    elif fault == "missing-manifest":
        state.fused_result.pop("manifest")
    elif fault == "null-manifest":
        state.fused_result["manifest"] = None
    else:
        state.fused_result["manifest"] = {}
    with pytest.raises((KeyError, TypeError, ValueError)):
        worker.run(state.request_file)
    assert_no_conversion(state)


@pytest.mark.parametrize("error", [RuntimeError("device unavailable"), OSError("disk full"), KeyboardInterrupt("cancelled")])
def test_fused_operational_failure_or_cancellation_never_converts(scenario, error):
    state = scenario
    state.fused_error = error
    with pytest.raises(type(error), match=str(error)):
        worker.run(state.request_file)
    assert_no_conversion(state)


def test_raw_change_after_fused_work_prevents_conversion(scenario):
    state = scenario
    # A changed size makes this deterministic even when the filesystem gives
    # two rapid writes the same modification timestamp.
    state.during_fused = lambda: (state.roots[0] / "recording.mcap").write_bytes(b"recording changed during preparation with extra bytes")
    with pytest.raises(ValueError, match="原始数据发生变化"):
        worker.run(state.request_file)
    assert_no_conversion(state)


def test_backend_validation_failure_does_not_publish(scenario):
    state = scenario
    state.validation = ValidationResult(False, ["missing video frame"])
    with pytest.raises(ValueError, match="deep validation 未通过"):
        worker.run(state.request_file)
    assert "convert" in state.calls and "validate" in state.calls and "publish" not in state.calls
    assert not state.destination.exists() and (state.job / "staging").exists()
    report = json.loads((state.job / "validation.json").read_text())
    assert report["valid"] is False and report["errors"] == ["missing video frame"]
    assert not any(event["event"] == "result" for event in state.events)


def test_backend_validation_exception_does_not_publish(scenario):
    state = scenario
    state.validation_error = RuntimeError("validator crashed")
    with pytest.raises(RuntimeError, match="validator crashed"):
        worker.run(state.request_file)
    assert "publish" not in state.calls and not state.destination.exists()
    assert not any(event["event"] == "result" for event in state.events)

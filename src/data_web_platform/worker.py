"""One child process per job. Only this process performs expensive conversion.

JSON events are authoritative, human logs remain available verbatim. All stages
share exactly the same immutable TianjiConfig. No synthetic frame percentages.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from .publishing import new_staging, publish_noreplace
from .conversion_summary import recording_durations, summarize, verify_manifest_selection


def emit(event, **fields):
    print("DWP_EVENT " + json.dumps({"event": event, **fields}, ensure_ascii=False, default=str), flush=True)


def fingerprint(roots):
    return [(str(p), p.stat().st_size, p.stat().st_mtime_ns)
            for root in roots for p in sorted(Path(root).rglob("*"))
            if p.is_file() and (p.suffix in {".mcap", ".yaml", ".json"})]


def run(request_file):
    """Check all selected inputs and convert only those passing strict rules.

    Keep one TianjiConfig for all stages. The combined provider call may write
    intermediate videos while checking. Data-quality failures are retained in
    the complete report and skipped; operational failures still abort the job.
    Only a deep-validated, correctly mapped subset is published to a new target.
    """
    request = json.loads(Path(request_file).read_text())
    job_dir = Path(request_file).parent
    os.nice(10)
    from tianji_provider.provider import TianjiConfig, provider
    from tianji_provider.diagnostics import check_dataset
    from tianji_provider.fused import check_and_prepare
    from letools.conversion import convert, ConversionConfig
    from letools.validation import validate_dataset

    cfg = TianjiConfig(**request["params"])
    selected = job_dir / "selected"
    selected.mkdir()
    for index, recording in enumerate(request["recordings"]):
        root = Path(recording)
        # Preserve recognizable names even for entirely empty/broken my_bag-*
        # recordings, so strict diagnostics cannot silently omit a selection.
        (selected / f"my_bag-{index:06d}-{root.name}").symlink_to(root, target_is_directory=True)
        session = root / "session.json"
        if session.exists():
            meta = json.loads(session.read_text())
            if meta.get("phase") not in (None, "saved", "closed"):
                raise ValueError(f"录制尚未保存完成: {root.name} ({meta.get('phase')})")
    before = fingerprint(request["recordings"])
    durations = recording_durations(request["recordings"])
    summary = summarize(durations)
    if request["kind"] == "convert":
        emit("conversion_summary", summary=summary)
    timings = {}

    def stage(name):
        emit("stage", name=name, status="running")
        return time.monotonic()

    def done(name, started):
        timings[name] = time.monotonic() - started
        emit("stage", name=name, status="succeeded", seconds=timings[name])

    def on_record(index, total, item):
        emit("check_result", completed=index, total=total, item=item)
    if request["kind"] == "check":
        phase = "check"
        started = stage(phase)
        report = check_dataset(selected, cfg, deep_video=request.get("deep_video", True),
                               workers=request.get("provider_workers"), progress=on_record)
        manifest = None
    else:
        phase = "check_prepare"
        started = stage(phase)
        fused = check_and_prepare(selected, job_dir / "cache", cfg, progress=on_record,
                                  workers=request.get("provider_workers"),
                                  video_encoder=request.get("video_encoder", "auto"),
                                  prepare_valid_only=True)
        report, manifest = fused["report"], fused["manifest"]
    (job_dir / "check.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    emit("check_report", report=report)
    if report["recordings"] != len(request["recordings"]) or len(report["results"]) != len(request["recordings"]):
        raise ValueError("检查数量与选择数量不同，拒绝遗漏任何轨迹")
    valid_count = sum(item["valid"] for item in report["results"])
    if valid_count != report["valid_recordings"] or len(durations) - valid_count != report["invalid_recordings"]:
        raise ValueError("检查报告汇总与逐条结果不一致")
    summary = summarize(durations, report)
    if request["kind"] == "convert":
        emit("conversion_summary", summary=summary)
    if fingerprint(request["recordings"]) != before:
        raise ValueError("检查/准备期间原始数据发生变化，请等待录制关闭后重新检查")
    if request["kind"] == "check" and not report["valid"]:
        raise ValueError(f"严格检查未通过：{report['invalid_recordings']} 条异常。可使用“检查并转换通过项”跳过异常轨迹；源数据保持不变。")
    if not valid_count:
        raise ValueError("全部所选轨迹均未通过检查，没有可转换的 episode；未生成目标数据集。")
    done(phase, started)
    if request["kind"] == "check":
        emit("result", result={"valid": True, "recordings": report["recordings"], "timings": timings})
        return
    if manifest is None:
        raise ValueError("融合检查通过但未生成完整缓存，拒绝转换")
    verify_manifest_selection(manifest, report)
    emit("prepared", episodes=len(manifest["episodes"]), timings=manifest.get("timings", {}))
    started = stage("convert")
    source = provider.open(job_dir / "cache", cfg)
    destination = Path(request["destination"])
    staging = new_staging(destination)
    emit("log", message=f"转换输出先写入独占暂存目录，验证后发布: {staging}")
    result = convert(source, staging, request["target_version"],
                     config=ConversionConfig(workers=request["workers"], video_workers=request["video_workers"]))
    done("convert", started)
    started = stage("validate")
    validation = validate_dataset(staging, deep=True)
    payload = asdict(validation)
    (job_dir / "validation.json").write_text(json.dumps(payload, indent=2, default=str))
    emit("validation", report=payload)
    if not validation.valid:
        raise ValueError(f"deep validation 未通过，未发布目标目录；诊断用暂存输出保留在 {staging}")
    info = json.loads((staging / "meta/info.json").read_text())
    if (info["total_episodes"] != valid_count or info["total_frames"] != report["valid_frames"]
            or info["fps"] != cfg.fps):
        raise ValueError("转换输出的轨迹数、帧数或 FPS 与通过项不一致，未发布目标目录")
    if fingerprint(request["recordings"]) != before:
        raise ValueError("转换期间原始数据发生变化，未发布目标目录")
    done("validate", started)
    started = stage("publish")
    publish_noreplace(staging, destination)
    done("publish", started)
    summary["output"] = {"episodes": info["total_episodes"], "frames": info["total_frames"],
                         "fps": info["fps"], "duration_seconds": info["total_frames"] / info["fps"]}
    emit("conversion_summary", summary=summary)
    payload["path"] = str(destination)
    (job_dir / "validation.json").write_text(json.dumps(payload, indent=2, default=str))
    emit("validation", report=payload)
    emit("result", result={**asdict(result), "destination": str(destination),
                           "timings": timings, "validation": payload, "conversion_summary": summary})


def main():
    try:
        run(sys.argv[1])
    except Exception as error:
        emit("error", message=str(error))
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

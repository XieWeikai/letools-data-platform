"""Isolated authenticated Doctor API acceptance with real worker subprocesses.

Example (no port binding, main-service restart, Hub writes, or source mutation):
    .venv/bin/python scripts/smoke_doctor.py --summary .state/verification-*/summary.json

Every run reserves a private evidence directory under .state, uses an independent
registry/job queue, and creates new fix/trim outputs only. Tokens are never logged.
Quality FAIL is a valid check/gate outcome; execution failure or an invalid output
is not. Exceptions produce a nonzero exit and retain all evidence for diagnosis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import av
from fastapi.testclient import TestClient

from data_web_platform.app import create_app
from data_web_platform.config import Config
from data_web_platform.datasets import dataset_fingerprint, inspect_dataset
from data_web_platform.doctor import CHECKS, FIXES, OPERATIONS
from letools.plugins import open_dataset
from letools.validation import validate_dataset


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def content_fingerprint(path):
    """Full byte hashes supplement the app's inexpensive metadata fingerprint."""
    digest = hashlib.sha256()
    for file in sorted(Path(path).rglob("*")):
        if file.is_symlink():
            raise ValueError(f"Smoke inputs must not contain symlinks: {file}")
        if file.is_file():
            digest.update(str(file.relative_to(path)).encode() + b"\0")
            with file.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
    return digest.hexdigest()


def verify_output(path, roots):
    """Independent full video decode, not just ffprobe or container metadata."""
    path, info = inspect_dataset(path, roots)
    validation = validate_dataset(path, deep=True)
    assert validation.valid, asdict(validation)
    source = open_dataset(path)
    fps = source.metadata.fps
    videos = {}
    for episode in source.episodes:
        for key, video in episode.videos.items():
            first = int(round(video.start * fps))
            assert abs(video.start - first / fps) <= .1 / fps
            record = videos.setdefault(video.path, {"frames": 0, "key": key})
            assert first == record["frames"], (video.path, first, record)
            record["frames"] += episode.length
    reports = []
    for file, expected in videos.items():
        count = 0
        with av.open(str(file)) as container:
            assert len(container.streams.video) == 1
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 2
            assert stream.codec_context.name == "h264", stream.codec_context.name
            feature = info["features"][expected["key"]]
            height, width = feature["shape"][:2]
            assert (stream.width, stream.height) == (width, height)
            for frame in container.decode(stream):
                assert frame.pts is not None
                timestamp = float(frame.pts * frame.time_base)
                assert math.isfinite(timestamp) and abs(timestamp - count / fps) <= .1 / fps
                assert (frame.width, frame.height) == (width, height)
                count += 1
        assert count == expected["frames"], (str(file), count, expected)
        reports.append({"path": str(file.relative_to(path)), "frames": count,
                        "width": width, "height": height, "codec": "h264"})
    assert reports, "Smoke sample must have real video streams"
    return {"deep_validation": asdict(validation), "videos": reports,
            "total_frames": info["total_frames"], "total_episodes": info["total_episodes"],
            "fps": fps, "version": info["codebase_version"]}


def run(args):
    root = Path(__file__).resolve().parents[1]
    if args.summary:
        previous = json.loads(Path(args.summary).read_text())
        origins = {version: Path(previous[version]["dataset"]["path"]).resolve()
                   for version in ("v2.1", "v3.0")}
    elif args.source_v21 and args.source_v30:
        origins = {"v2.1": Path(args.source_v21).resolve(), "v3.0": Path(args.source_v30).resolve()}
    else:
        raise ValueError("Provide --summary or both --source-v21 and --source-v30")
    evidence_parent = root / ".state"
    evidence_parent.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="doctor-verification-" + time.strftime("%Y%m%d-%H%M%S-") , dir=evidence_parent))
    # Browser QA may intentionally edit annotations in the shared sample. Use
    # independent ordinary-file snapshots so parallel suites never share their
    # input registry or mutable sample. Never use hardlinks or reflink tricks.
    sources, origin_snapshots = {}, {}
    for version, origin in origins.items():
        inspect_dataset(origin, [origin])
        original_fingerprint = dataset_fingerprint(origin)
        original_sha256 = content_fingerprint(origin)  # Also refuses symlinks.
        target = evidence / "inputs" / version
        shutil.copytree(origin, target, symlinks=False, copy_function=shutil.copy2)
        assert dataset_fingerprint(origin) == original_fingerprint, "Origin changed while taking the private snapshot"
        assert content_fingerprint(target) == original_sha256, "Private input snapshot differs from its origin"
        sources[version] = target
        origin_snapshots[version] = {"path": str(origin), "metadata": original_fingerprint,
                                     "sha256": original_sha256}
    config = Config(project_root=root, state_dir=evidence / "runtime", python=Path(sys.executable),
                    allowed_roots=[evidence, *sources.values()])
    app = create_app(config, start_services=False)
    jobs = app.state.jobs
    result = {"evidence": str(evidence), "sources": {v: str(p) for v, p in sources.items()},
              "input_origins_at_snapshot": origin_snapshots,
              "operations": {}, "outputs": {}, "safety": {}, "passed": False}
    before = {v: {"metadata": dataset_fingerprint(p), "sha256": content_fingerprint(p)}
              for v, p in sources.items()}
    started = time.monotonic()
    print("DOCTOR_VERIFICATION " + str(evidence), flush=True)

    def save():
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(evidence / "summary.json", result)

    try:
        with TestClient(app) as client:
            assert client.get("/api/doctor/options").status_code == 401
            response = client.post("/api/session", json={"token": config.auth_token})
            assert response.status_code == 200, "Isolated API authentication failed"
            options = client.get("/api/doctor/options")
            options.raise_for_status()
            assert set(options.json()["operations"]) == set(OPERATIONS)
            assert options.json()["checks"] == list(CHECKS) and len(CHECKS) == 12
            assert options.json()["fixes"] == list(FIXES)
            write_json(evidence / "options.json", options.json())
            records = {}
            for version, path in sources.items():
                response = client.post("/api/datasets", json={"path": str(path)})
                response.raise_for_status()
                records[version] = response.json()
                assert records[version]["info"]["codebase_version"] == version
            jobs.start()

            def perform(label, payload, *, quality_failure_allowed=False):
                response = client.post("/api/doctor/jobs", json=payload)
                response.raise_for_status()
                job_id = response.json()["id"]
                deadline = time.monotonic() + args.timeout
                prior = None
                while True:
                    response = client.get(f"/api/jobs/{job_id}")
                    response.raise_for_status()
                    job = response.json()
                    status = (job["status"], job["stage"])
                    if status != prior:
                        print(json.dumps({"operation": label, "job": job_id,
                                          "status": status[0], "stage": status[1],
                                          "seconds": job["elapsed_seconds"]}), flush=True)
                        prior = status
                    if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                        write_json(evidence / f"{label}.json", job)
                        outcome = job.get("result")
                        result["operations"][label] = {"id": job_id, "status": job["status"],
                            "seconds": job["elapsed_seconds"], "quality_passed": outcome.get("passed") if outcome else None}
                        save()
                        assert outcome and "report" in outcome, f"{label} did not return a structured result; see evidence"
                        assert job["status"] == "succeeded" or (quality_failure_allowed and job["status"] == "failed" and outcome["passed"] is False), f"{label} failed; see evidence"
                        return job
                    if time.monotonic() >= deadline:
                        client.post(f"/api/jobs/{job_id}/cancel")
                        raise TimeoutError(f"{label} exceeded {args.timeout}s")
                    time.sleep(.5)

            for version, record in records.items():
                identity = record["repo_id"]
                response = client.get("/doctor", params={"dataset": identity})
                assert response.status_code == 200 and "Doctor" in response.text
                response = client.get("/api/doctor/report", params={"dataset": identity})
                response.raise_for_status()
                report = response.json()
                assert len(report["checks"]) == 12
                write_json(evidence / f"report-{version}.json", report)
                repeated = client.get("/doctor/report.json", params={"dataset": identity})
                assert repeated.json()["cached"] is True
                for operation in ("check", "score", "gate"):
                    job = perform(f"{operation}-{version}", {"dataset": identity, "operation": operation},
                                  quality_failure_allowed=operation in {"check", "gate"})
                    operation_report = job["result"]["report"]
                    if "observation.action" in record["info"]["features"] and "action" not in record["info"]["features"]:
                        assert operation_report["compatibility"]["source"] == "observation.action"
                        assert "No action features" not in json.dumps(operation_report)
                        if operation == "score":
                            assert len(operation_report["scores"]) == record["info"]["total_episodes"]
                if args.readonly_only:
                    continue
                perform(f"post-merge-check-{version}", {"operation": "merge-check",
                    "datasets": [identity], "post_merge": True}, quality_failure_allowed=True)
                preview = evidence / f"preview-{version}"
                perform(f"trim-preview-{version}", {"operation": "trim", "dataset": identity,
                    "output": str(preview), "confirm": True, "dry_run": True,
                    "threshold": .001, "min_active": 1, "keep_static": True})
                assert not preview.exists()
                for operation in ("fix", "trim"):
                    output = evidence / f"{operation}-{version}"
                    payload = {"operation": operation, "dataset": identity,
                               "output": str(output), "confirm": True}
                    if operation == "fix":
                        payload["fixes"] = list(FIXES)
                    else:
                        payload.update(threshold=.001, min_active=1, keep_static=True)
                    job = perform(f"{operation}-{version}", payload)
                    assert job["result"].get("dataset", {}).get("path") == str(output), "Output was not registered"
                    validation = verify_output(output, config.allowed_roots)
                    result["outputs"][f"{operation}-{version}"] = validation
                    # Reject overwrites, not just concurrent active jobs.
                    assert client.post("/api/doctor/jobs", json=payload).status_code == 400
                    assert dataset_fingerprint(sources[version]) == before[version]["metadata"]
                    save()
                assert client.post("/api/doctor/jobs", json={"operation": "fix", "dataset": identity,
                    "confirm": True, "output": str(sources[version] / "unsafe-output")}).status_code == 400
                assert client.post("/api/doctor/jobs", json={"operation": "trim", "dataset": identity,
                    "confirm": False, "output": str(evidence / "unconfirmed")}).status_code == 422
            result["safety"]["authentication"] = True
            if not args.readonly_only:
                perform("merge-check-v21-v30", {"operation": "merge-check",
                    "datasets": [r["repo_id"] for r in records.values()]}, quality_failure_allowed=True)
                result["safety"].update(overwrite_rejected=True,
                    nested_source_output_rejected=True, confirmation_required=True,
                    dry_run_no_output=True, all_new_outputs_registered=True)
        after = {v: {"metadata": dataset_fingerprint(p), "sha256": content_fingerprint(p)}
                 for v, p in sources.items()}
        assert before == after, "Source dataset fingerprint changed"
        result["source_fingerprints"] = {"before": before, "after": after}
        result["safety"]["source_bytes_unchanged"] = True
        result["passed"] = True
        save()
        print(json.dumps({"passed": True, "operations": len(result["operations"]),
                          "outputs": len(result["outputs"]), "seconds": result["elapsed_seconds"],
                          "summary": str(evidence / "summary.json")}), flush=True)
        return 0
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        save()
        raise
    finally:
        jobs.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--source-v21", type=Path)
    parser.add_argument("--source-v30", type=Path)
    parser.add_argument("--timeout", type=float, default=1800, help="Per-job timeout, seconds")
    parser.add_argument("--readonly-only", action="store_true", help="Only check/score/gate (no fix/trim outputs)")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

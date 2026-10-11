"""Cookie-free live API check + real v2.1/v3.0 conversions (new outputs only).

Run on the server after starting the service. Does not touch original metadata,
does not require login credentials, and writes all evidence under private state.
"""
from __future__ import annotations
import argparse
import json
import os
import tempfile
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:8095")
    parser.add_argument("--source", required=True, help="Known-good recording on this server")
    parser.add_argument("--negative", help="Known-bad recording; required unless explicitly skipped")
    parser.add_argument("--task", required=True, help="Actual task instruction for the chosen recording")
    parser.add_argument("--timeout", type=float, default=900, help="Seconds per job; timeout does not cancel the job")
    parser.add_argument("--skip-negative", action="store_true")
    parser.add_argument("--versions", nargs="+", choices=["v2.1", "v3.0"], default=["v2.1", "v3.0"])
    args = parser.parse_args()
    if not Path(args.source).is_dir():
        parser.error("--source must be an existing server recording directory")
    if not args.skip_negative and (not args.negative or not Path(args.negative).is_dir()):
        parser.error("Provide an existing --negative recording or explicitly use --skip-negative")
    if args.timeout <= 0 or not args.task.strip():
        parser.error("timeout and task must be positive/nonblank")
    root = Path(__file__).resolve().parents[1]
    state = Path(os.environ.get("DWP_STATE_DIR", root / ".state"))
    state.mkdir(parents=True, exist_ok=True)
    test = Path(tempfile.mkdtemp(prefix="verification-", dir=state))
    client = httpx.Client(base_url=args.origin, timeout=60)
    assert not client.cookies, "API smoke must start without session cookies"
    environment = client.get("/api/environment")
    environment.raise_for_status()
    assert "set-cookie" not in environment.headers, "Public API must not create a login session"
    (test / "environment.json").write_text(json.dumps(environment.json(), indent=2))
    results = {}

    def run(payload):
        response = client.post("/api/jobs", json=payload)
        response.raise_for_status()
        job_id = response.json()["id"]
        print(f"JOB {job_id} {payload['kind']} {payload.get('target_version', '')}", flush=True)
        previous = None
        deadline = time.monotonic() + args.timeout
        while True:
            response = client.get(f"/api/jobs/{job_id}")
            response.raise_for_status()
            job = response.json()
            marker = (job["status"], job["stage"], job["progress"].get("completed"))
            if marker != previous:
                print(json.dumps({"job": job_id, "status": job["status"], "stage": job["stage"],
                                  "progress": job["progress"], "elapsed": job["elapsed_seconds"]}), flush=True)
                previous = marker
            if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                (test / f"{job_id}.json").write_text(json.dumps(job, indent=2, ensure_ascii=False))
                return job
            if time.monotonic() > deadline:
                raise TimeoutError(f"Job {job_id} is still live; inspect it before retrying")
            time.sleep(2)

    if not args.skip_negative:
        negative = run({"kind": "check", "source_root": args.negative, "recordings": [args.negative]})
        assert negative["status"] == "failed" and negative["check_report"] and not negative["check_report"]["valid"], negative
        results["strict_negative"] = negative["id"]
    for version in args.versions:
        output = test / ("converted-" + version)
        result = run({"kind": "convert", "source_root": args.source, "recordings": [args.source],
                      "destination": str(output), "target_version": version,
                      "params": {"task": args.task, "fps": 50}})
        assert result["status"] == "succeeded", result
        dataset = result["result"]["dataset"]
        check = client.get("/api/doctor/report", params={"dataset": dataset["id"]}, timeout=180)
        check.raise_for_status()
        (test / f"doctor-{version}.json").write_text(json.dumps(check.json(), indent=2))
        results[version] = {"job": result["id"], "dataset": dataset, "seconds": result["elapsed_seconds"],
                            "result": result["result"]}
    (test / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print("VERIFICATION " + str(test), flush=True)


if __name__ == "__main__":
    main()

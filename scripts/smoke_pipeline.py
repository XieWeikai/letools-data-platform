"""Cookie-free live API check + real v2.1/v3.0 conversions (new outputs only).

Run on the server after starting the service. Does not touch original metadata,
does not require login credentials, and writes all evidence under private state.
"""
from __future__ import annotations
import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:8095")
    parser.add_argument("--source", default="/media/user/BAG_STORAGE/recorded_bags/my_bag-26-09-16-15-55-24-ff0cc597")
    parser.add_argument("--negative", default="/media/user/BAG_STORAGE/recorded_bags/my_bag-26-09-16-15-56-37-27f8ef7d")
    parser.add_argument("--skip-negative", action="store_true")
    parser.add_argument("--versions", nargs="+", choices=["v2.1", "v3.0"], default=["v2.1", "v3.0"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    state = root / ".state"
    test = state / ("verification-" + time.strftime("%Y%m%d-%H%M%S"))
    test.mkdir(parents=True)
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
            time.sleep(2)

    if not args.skip_negative and Path(args.negative).exists():
        negative = run({"kind": "check", "source_root": args.negative, "recordings": [args.negative]})
        assert negative["status"] == "failed" and negative["check_report"] and not negative["check_report"]["valid"], negative
        results["strict_negative"] = negative["id"]
    for version in args.versions:
        output = test / ("converted-" + version)
        result = run({"kind": "convert", "source_root": args.source, "recordings": [args.source],
                      "destination": str(output), "target_version": version,
                      "params": {"task": "Plug in the Ethernet cable", "fps": 50}})
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

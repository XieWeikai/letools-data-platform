import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from data_web_platform.config import Config
from data_web_platform.jobs import JobManager, _ProcessGroup
import data_web_platform.jobs as jobs_module


def manager(tmp_path):
    config = Config(project_root=tmp_path, state_dir=tmp_path / "state", allowed_roots=[tmp_path])
    config.initialize()
    return JobManager(config)


def test_status_persistence_cancel_and_restart(tmp_path):
    jobs = manager(tmp_path)
    one = jobs.submit({"kind": "check", "recordings": ["example"]})
    assert jobs.get(one["id"])["status"] == "queued"
    assert jobs.cancel(one["id"])["status"] == "cancelled"
    two = jobs.submit({"kind": "check", "recordings": ["example"]})
    restored = manager(tmp_path)
    assert restored.get(one["id"])["status"] == "cancelled"
    assert restored.get(two["id"])["status"] == "interrupted"


def test_real_events_keep_info_diagnostics_and_failures(tmp_path):
    jobs = manager(tmp_path)
    job = jobs.submit({"kind": "convert"})
    state = jobs.records[job["id"]]
    jobs._event(state, {"event": "stage", "name": "check", "status": "running"})
    jobs._event(state, {"event": "check_result", "completed": 1, "total": 2,
                       "item": {"bag": "one", "valid": True, "issues": [{"severity": "info"}]}})
    assert state["progress"]["completed"] == 1
    assert state["check_results"][0]["issues"][0]["severity"] == "info"
    jobs._event(state, {"event": "error", "message": "gap detected"})
    assert state["error"] == "gap detected"


def test_conversion_summary_survives_service_restart(tmp_path):
    jobs = manager(tmp_path)
    job = jobs.submit({"kind": "convert"})
    item = jobs.records[job["id"]]
    summary = {"policy": "valid_only", "input": {"episodes": 3, "duration_seconds": 45},
               "skipped": {"episodes": 1}, "output": {"episodes": 2, "duration_seconds": 20}}
    jobs._event(item, {"event": "conversion_summary", "summary": summary})
    item["status"] = "succeeded"
    jobs._save(item)
    assert manager(tmp_path).get(job["id"])["conversion_summary"] == summary


def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    pytest.fail("background job did not reach expected state")


def scripted_workers(monkeypatch, scripts):
    """Use actual isolated processes, without importing conversion dependencies."""
    original = subprocess.Popen
    processes = []
    scripts = iter(scripts)

    def spawn(command, *args, **kwargs):
        if "data_web_platform.worker" in command:
            process = original([sys.executable, "-u", "-c", next(scripts)], *args, **kwargs)
            processes.append(process)
            return process
        return original(command, *args, **kwargs)

    monkeypatch.setattr(jobs_module.subprocess, "Popen", spawn)
    monkeypatch.setattr(jobs_module, "CANCEL_GRACE_SECONDS", .05)
    return processes


SUCCESS = 'print(\'DWP_EVENT {"event":"result","result":{"valid":true}}\', flush=True)'
WAITING = 'import time; print("trigger", flush=True); time.sleep(60)'


def process_alive(pid):
    # A killed descendant may briefly remain a zombie until the OS adopts/reaps it.
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (FileNotFoundError, ProcessLookupError):
            return False
    result = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True)
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux procfs disappearance race")
@pytest.mark.parametrize("missing", [FileNotFoundError, ProcessLookupError])
def test_process_alive_handles_exit_during_procfs_read(monkeypatch, missing):
    def vanished(*args, **kwargs):
        raise missing("process exited during /proc read")

    monkeypatch.setattr(Path, "read_text", vanished)
    assert process_alive(12345) is False


def test_elapsed_uses_monotonic_not_wall_clock(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    job = jobs.submit({"kind": "check"})
    jobs.records[job["id"]].update(status="running", started_time=9000)
    jobs.started_monotonic[job["id"]] = 100
    monkeypatch.setattr(jobs_module.time, "time", lambda: -1_000_000)
    monkeypatch.setattr(jobs_module.time, "monotonic", lambda: 104.25)
    assert jobs.get(job["id"])["elapsed_seconds"] == 4.2
    assert jobs.cancel(job["id"])["elapsed_seconds"] == 4.25


def test_persistence_failure_kills_worker_and_queue_continues(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    first = jobs.submit({"kind": "check"})
    second = jobs.submit({"kind": "check"})
    original_save = jobs._save

    def failing_save(item):
        if item["id"] == first["id"] and "trigger" in item["logs"]:
            raise OSError("No space left on device")
        original_save(item)

    monkeypatch.setattr(jobs, "_save", failing_save)
    processes = scripted_workers(monkeypatch, [WAITING, SUCCESS])
    jobs.start()
    try:
        wait_for(lambda: jobs.get(second["id"])["status"] == "succeeded")
        failed = jobs.get(first["id"])
        assert failed["status"] == "failed"
        assert "No space left" in failed["error"]
        assert "No space left" in failed["persistence_error"]
        assert processes[0].returncode is not None
        assert not jobs.processes
        assert jobs.thread.is_alive()
    finally:
        jobs.close()


def test_log_write_failure_also_reaps_and_continues(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    first = jobs.submit({"kind": "check"})
    second = jobs.submit({"kind": "check"})
    original_open = Path.open
    bad_log = jobs.root / first["id"] / "worker.log"

    def failing_open(path, *args, **kwargs):
        if path == bad_log:
            stream = MagicMock()
            stream.__enter__.return_value = stream
            stream.write.side_effect = OSError("worker log disk full")
            return stream
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    processes = scripted_workers(monkeypatch, [WAITING, SUCCESS])
    jobs.start()
    try:
        wait_for(lambda: jobs.get(second["id"])["status"] == "succeeded")
        assert "worker log disk full" in jobs.get(first["id"])["error"]
        assert processes[0].returncode is not None
        assert not jobs.processes
    finally:
        jobs.close()


def test_launch_failure_does_not_stop_queue(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    first = jobs.submit({"kind": "check"})
    second = jobs.submit({"kind": "check"})
    scripted_workers(monkeypatch, [SUCCESS])
    working_spawn = subprocess.Popen
    attempts = 0

    def fail_once(command, *args, **kwargs):
        nonlocal attempts
        if "data_web_platform.worker" in command:
            attempts += 1
            if attempts == 1:
                raise OSError("cannot spawn worker")
        return working_spawn(command, *args, **kwargs)

    monkeypatch.setattr(jobs_module.subprocess, "Popen", fail_once)
    jobs.start()
    try:
        wait_for(lambda: jobs.get(second["id"])["status"] == "succeeded")
        assert jobs.get(first["id"])["status"] == "failed"
        assert not jobs.started_monotonic
    finally:
        jobs.close()


def test_cancel_kills_child_even_after_leader_exits(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    job = jobs.submit({"kind": "check"})
    child = ('import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             'print("child-ready", flush=True); time.sleep(60)')
    script = (f'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-u","-c",{child!r}]); '
              'print("child-pid",p.pid,flush=True); time.sleep(60)')
    processes = scripted_workers(monkeypatch, [script])
    jobs.start()
    try:
        wait_for(lambda: "child-ready" in jobs.get(job["id"])["logs"])
        child_pid = int(next(line.split()[1] for line in jobs.get(job["id"])["logs"]
                             if line.startswith("child-pid ")))
        assert jobs.cancel(job["id"])["status"] == "cancelled"
        wait_for(lambda: not jobs.processes)
        wait_for(lambda: not process_alive(child_pid))
        assert processes[0].returncode == -signal.SIGTERM
    finally:
        jobs.close()


def test_normal_exit_cleans_detached_stdout_descendant(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    job = jobs.submit({"kind": "check"})
    child = 'import time; time.sleep(60)'
    script = (f'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",{child!r}], '
              'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
              'print("child-pid",p.pid,flush=True); ' + SUCCESS)
    processes = scripted_workers(monkeypatch, [script])
    jobs.start()
    try:
        wait_for(lambda: jobs.get(job["id"])["status"] == "succeeded")
        child_pid = int(next(line.split()[1] for line in jobs.get(job["id"])["logs"]
                             if line.startswith("child-pid ")))
        wait_for(lambda: not process_alive(child_pid))
        assert processes[0].returncode == 0
    finally:
        jobs.close()


def test_group_never_signals_after_reaping_or_wrong_group(monkeypatch):
    process = MagicMock(pid=987654, returncode=None, stdout=None)
    monkeypatch.setattr(jobs_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(jobs_module.os, "getsid", lambda pid: pid)
    kill = MagicMock()
    monkeypatch.setattr(jobs_module.os, "killpg", kill)
    monkeypatch.setattr(jobs_module, "_wait_unreaped", lambda proc: True)
    process.wait.side_effect = lambda: setattr(process, "returncode", 0)
    group = _ProcessGroup(process)
    assert group.finish() == 0
    assert kill.call_args.args == (process.pid, signal.SIGKILL)
    count = kill.call_count
    group.cancel()
    group._signal(signal.SIGKILL)
    assert kill.call_count == count
    process.returncode = None
    another = _ProcessGroup(process)
    monkeypatch.setattr(jobs_module.os, "getpgid", lambda pid: pid + 1)
    another._signal(signal.SIGTERM)
    assert kill.call_count == count


def test_group_exit_race_is_harmless(monkeypatch):
    process = MagicMock(pid=987654, returncode=None)
    monkeypatch.setattr(jobs_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(jobs_module.os, "getsid", lambda pid: pid)
    monkeypatch.setattr(jobs_module.os, "killpg", MagicMock(side_effect=ProcessLookupError))
    _ProcessGroup(process)._signal(signal.SIGTERM)


def test_submit_save_failure_does_not_leave_phantom_job(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    monkeypatch.setattr(jobs, "_save", MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        jobs.submit({"kind": "check"})
    assert not jobs.records
    assert jobs.pending.empty()


def test_restore_keeps_interrupted_job_if_disk_cannot_save(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    job = jobs.submit({"kind": "check"})
    monkeypatch.setattr(jobs_module, "atomic_json", MagicMock(side_effect=OSError("disk full")))
    restored = manager(tmp_path)
    assert restored.get(job["id"])["status"] == "interrupted"
    assert restored.get(job["id"])["persistence_error"] == "disk full"

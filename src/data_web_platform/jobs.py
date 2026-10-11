"""Durable, single-lane jobs with subprocess isolation and process-group cancel."""
from __future__ import annotations

import copy
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
CANCEL_GRACE_SECONDS = 5


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temp.replace(path)


def _child_state(pid):
    """macOS fallback: only inspect a PID which is still our direct child."""
    result = subprocess.run(["/bin/ps", "-o", "ppid=,stat=", "-p", str(pid)],
                            capture_output=True, text=True, check=False)
    values = result.stdout.split()
    if result.returncode or len(values) != 2 or values[0] != str(os.getpid()):
        return ""
    return values[1]


def _is_unreaped_exit(process):
    if process.returncode is not None:
        return False
    if hasattr(os, "waitid"):
        try:
            event = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            return event is not None and event.si_pid == process.pid
        except (ChildProcessError, InterruptedError):
            return False
    return _child_state(process.pid).startswith("Z")


def _wait_unreaped(process):
    """Wait without freeing the PID which anchors our exclusively owned group.

    The Linux service uses waitid/WNOWAIT. macOS development Python does not
    expose waitid; ps observes the zombie without reaping it. No other code may
    call Popen.poll/wait before this group's final cleanup.
    """
    if process.returncode is not None:
        return False
    if hasattr(os, "waitid"):
        while True:
            try:
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
                return True
            except InterruptedError:
                continue
            except ChildProcessError:
                return False  # An external reaper invalidated ownership.
    while True:
        state = _child_state(process.pid)
        if not state:
            return False
        if state.startswith("Z"):
            return True
        time.sleep(.02)


class _ProcessGroup:
    """One signal owner and one reaper; never signal after PID reuse is possible."""

    def __init__(self, process):
        self.process = process
        self.lock = threading.RLock()
        self.finished = threading.Event()
        self.cancel_requested = False
        self.closed = False

    def _signal(self, signum):
        with self.lock:
            if self.closed or self.process.returncode is not None:
                return
            try:
                # start_new_session=True gives this child its own SID and PGID.
                # The leader is not reaped until finish(), so its PID cannot
                # become another process between this check and killpg.
                owned = (os.getpgid(self.process.pid) == self.process.pid
                         and os.getsid(self.process.pid) == self.process.pid)
            except ProcessLookupError:
                # macOS cannot query the group of a zombie. Its still-waitable
                # child identity pins the original PID just as on Linux.
                owned = _is_unreaped_exit(self.process)
            if not owned:
                return
            try:
                os.killpg(self.process.pid, signum)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin reports EPERM (Linux ESRCH) for a process group whose
                # only remaining member is its zombie leader. The unreaped
                # direct child still pins this PID; there is nothing to kill.
                if sys.platform != "darwin" or not _is_unreaped_exit(self.process):
                    raise

    def cancel(self):
        with self.lock:
            if self.closed or self.cancel_requested:
                return
            self.cancel_requested = True
            self._signal(signal.SIGTERM)
            threading.Thread(target=self._force_stop, name="dataset-job-cancel", daemon=True).start()

    def _force_stop(self):
        if not self.finished.wait(CANCEL_GRACE_SECONDS):
            # Do not poll/wait the leader: it may have exited while a child
            # which ignores SIGTERM still holds stdout or writes output.
            self._signal(signal.SIGKILL)

    def finish(self, *, abort=False):
        owned = False
        try:
            if abort:
                self.cancel()
            owned = _wait_unreaped(self.process)
        except BaseException:
            self._signal(signal.SIGKILL)
            raise
        finally:
            with self.lock:
                try:
                    if owned:
                        self._signal(signal.SIGKILL)  # Includes detached-stdout descendants.
                finally:
                    self.closed = True
                    self.finished.set()
                    try:
                        self.process.wait()
                    finally:
                        if self.process.stdout is not None:
                            self.process.stdout.close()
        return self.process.returncode


class JobManager:
    """Persist one queue and serialize all conversion/Doctor/editor processes.

    On restart, unfinished records become interrupted instead of being retried
    against potentially changed inputs. Each worker gets its own process group;
    cancellation and shutdown use the same owner to avoid signalling reused PIDs.
    """
    def __init__(self, config, registry=None):
        self.config, self.registry = config, registry
        self.root = config.state_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.records = {}
        self.pending = queue.Queue()
        self.processes = {}
        self.process_groups = {}
        self.started_monotonic = {}
        self.stopping = threading.Event()
        for path in sorted(self.root.glob("*/status.json")):
            try:
                item = json.loads(path.read_text())
                if item["status"] not in TERMINAL:
                    item.update(status="interrupted", error="服务重启中断了任务；请检查日志后重新提交。", updated_at=now())
                    self._best_effort_save(item)
                self.records[item["id"]] = item
            except (ValueError, KeyError, OSError):
                continue
        self.thread = threading.Thread(target=self._loop, name="dataset-jobs", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            pending = [job_id for job_id, item in self.records.items() if item["status"] not in TERMINAL]
        for job_id in pending:
            self.cancel(job_id)
        self.pending.put(None)
        if self.thread.is_alive():
            self.thread.join(timeout=8)

    def _save(self, item):
        item["updated_at"] = now()
        atomic_json(self.root / item["id"] / "status.json", item)

    def _best_effort_save(self, item):
        """A full/read-only disk must not take down the sole scheduler thread."""
        try:
            self._save(item)
        except OSError as error:
            message = f"状态持久化失败（内存状态仍可查看）: {error}"
            item["persistence_error"] = str(error)
            if not item["logs"] or item["logs"][-1] != message:
                item["logs"] = [*item["logs"][-199:], message]

    def _elapsed(self, job_id):
        started = self.started_monotonic.get(job_id)
        return max(0, time.monotonic() - started) if started is not None else 0

    def _submit(self, request, module):
        """Save an immutable request before enqueueing and reject output races."""
        with self.lock:
            output = request.get("destination") or request.get("output")
            if output:
                for item in self.records.values():
                    reserved = item["request"].get("destination") or item["request"].get("output")
                    if item["status"] not in TERMINAL and reserved == output:
                        raise ValueError("该输出目录已被另一个待处理任务使用")
            job_id = uuid.uuid4().hex
            directory = self.root / job_id
            directory.mkdir(mode=0o700)
            atomic_json(directory / "request.json", request)
            item = dict(id=job_id, kind=request.get("kind", "doctor"), status="queued", stage="queued",
                        progress={}, created_at=now(), updated_at=now(), elapsed_seconds=0,
                        error=None, check_report=None, check_results=[], stages=[], result=None,
                        logs=[], request=request, module=module)
            self._save(item)
            self.records[job_id] = item
            self.pending.put(job_id)
            return copy.deepcopy(item)

    def submit(self, request):
        return self._submit(request, "data_web_platform.worker")

    def submit_doctor(self, request):
        return self._submit({**request, "kind": "doctor"}, "data_web_platform.doctor_worker")

    def submit_editor(self, request):
        return self._submit(request, "data_web_platform.editor_worker")

    def get(self, job_id):
        with self.lock:
            if job_id not in self.records:
                raise KeyError(job_id)
            item = copy.deepcopy(self.records[job_id])
            if job_id in self.started_monotonic and item["status"] not in TERMINAL:
                item["elapsed_seconds"] = round(self._elapsed(job_id), 1)
            return item

    def all(self):
        with self.lock:
            return [self.get(k) for k in reversed(list(self.records))]

    def cancel(self, job_id):
        with self.lock:
            item = self.records[job_id]
            if item["status"] in TERMINAL:
                return self.get(job_id)
            item["status"] = "cancelled"
            item["error"] = "用户取消；原始数据未修改，任务缓存保留以便排查。"
            item["elapsed_seconds"] = round(self._elapsed(job_id), 2)
            group = self.process_groups.get(job_id)
            if group is not None:
                group.cancel()
            self._best_effort_save(item)
            return self.get(job_id)

    def _event(self, item, event):
        """Reduce structured worker events into the persistent UI projection.

        Stage/check counters come from actual events. Logs are separate text;
        a progress display is never a substitute for a successful worker result.
        """
        kind = event.get("event")
        if kind == "stage":
            name = event["name"]
            item["stage"] = name
            stage = next((s for s in item["stages"] if s["name"] == name), None)
            if stage is None:
                stage = {"name": name}
                item["stages"].append(stage)
            stage.update({k: v for k, v in event.items() if k != "event"})
            if event.get("status") == "running":
                item["progress"] = {}
        elif kind == "check_result":
            item["check_results"].append(event["item"])
            item["progress"] = {"completed": event["completed"], "total": event["total"],
                                "current": event["item"].get("bag", "")}
        elif kind == "check_report":
            item["check_report"] = event["report"]
        elif kind in {"result", "validation"}:
            item[kind] = event.get("result", event.get("report"))
        elif kind == "progress":
            item["progress"] = {k: v for k, v in event.items() if k != "event"}
        elif kind == "error":
            item["error"] = event["message"]

    def _loop(self):
        while not self.stopping.is_set():
            job_id = self.pending.get()
            if job_id is None:
                self.pending.task_done()
                break
            try:
                self._run(job_id)
            except Exception as error:
                with self.lock:
                    item = self.records[job_id]
                    if item["status"] != "cancelled":
                        item.update(status="failed", error=f"任务管理器: {error}")
                    for stage in item["stages"]:
                        if stage.get("status") == "running":
                            stage["status"] = item["status"]
                    self._best_effort_save(item)
            finally:
                self.pending.task_done()

    def _run(self, job_id):
        """Drain worker output, reap the owned group, then settle/register result.

        Spawning with an argv list avoids shell interpretation of dataset text.
        Source and output options were validated before this point and are
        rechecked by workers; user input cannot choose a module to execute.
        """
        directory = self.root / job_id
        group = None
        complete = False
        try:
            with self.lock:
                item = self.records[job_id]
                if item["status"] == "cancelled":
                    return
                item.update(status="running", started_time=time.time())
                self.started_monotonic[job_id] = time.monotonic()
                self._save(item)
                process = subprocess.Popen(
                    [str(self.config.python), "-u", "-m", item["module"], str(directory / "request.json")],
                    cwd=self.config.project_root, env=self.config.environment(),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                    start_new_session=True)
                group = _ProcessGroup(process)
                self.processes[job_id] = process
                self.process_groups[job_id] = group
            with (directory / "worker.log").open("a", buffering=1) as logfile:
                for line in process.stdout:
                    logfile.write(line)
                    text = line.rstrip()
                    with self.lock:
                        if text.startswith("DWP_EVENT "):
                            try:
                                event = json.loads(text[len("DWP_EVENT "):])
                                if not isinstance(event, dict):
                                    raise ValueError("Event must be an object")
                                self._event(item, event)
                            except (ValueError, KeyError, TypeError):
                                item["logs"].append(text[:8000])
                        else:
                            item["logs"].append(text[:8000])
                            if item["stage"] == "prepare" and text.lstrip().startswith("Encoded "):
                                count = item["progress"].get("completed", 0) + 1
                                item["progress"] = {"completed": count, "total": len(item["request"]["recordings"]), "current": text[:200]}
                        item["logs"] = item["logs"][-200:]
                        self._save(item)
            complete = True
        finally:
            try:
                if group is not None:
                    code = group.finish(abort=not complete)
                    item["returncode"] = code
            finally:
                with self.lock:
                    self.processes.pop(job_id, None)
                    self.process_groups.pop(job_id, None)
                    if job_id in self.started_monotonic:
                        item["elapsed_seconds"] = round(self._elapsed(job_id), 2)
                        self.started_monotonic.pop(job_id, None)
        with self.lock:
            if item["status"] != "cancelled":
                item["status"] = "succeeded" if code == 0 and item["result"] is not None else "failed"
                if item["status"] == "failed" and not item["error"]:
                    item["error"] = f"子进程未正常完成 (exit {code})；请查看日志"
            for stage in item["stages"]:
                if stage.get("status") == "running":
                    stage["status"] = item["status"]
            if item["status"] == "succeeded" and self.registry and item["request"].get("destination"):
                try:
                    dataset = self.registry.register(item["request"]["destination"])
                    item["result"]["dataset"] = dataset
                except (ValueError, OSError) as error:
                    item["logs"].append(f"自动添加到数据集列表失败：{error}")
            self._best_effort_save(item)

"""Bounded background indexing and read-only, path-confined raw-data API."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import re
import subprocess
import threading

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from .paths import validate_path


class RawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class RawViewer:
    """One low-priority index process; capped queue and fingerprinted disk cache.

    No global LeTools/provider configuration changes; source code is invoked
    using the configured private interpreter. Completed indexes survive restart.
    """
    def __init__(self, config):
        self.config = config
        self.root = config.state_dir / "raw-viewer"
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raw-viewer")
        self.pending = set()
        self.process = None
        self.closed = False

    def open(self, path):
        from .raw_data import source_snapshot
        snapshot = source_snapshot(path, self.config.allowed_roots)
        if Path(snapshot["path"]).is_relative_to(self.config.state_dir):
            raise ValueError("请选择原始录制，不能索引平台工作缓存")
        token = snapshot["id"]
        with self.lock:
            if self.closed:
                raise HTTPException(503, "服务正在停止")
            directory = self.root / token
            if (directory / "manifest.json").is_file():
                return self.status(token)
            if token not in self.pending:
                if len(self.pending) >= 4:
                    raise HTTPException(429, "原始预览正在处理其他录制，请稍后重试")
                if directory.exists():
                    # Never reuse a half-written index after a crash.
                    import tempfile
                    diagnostic = Path(tempfile.mkdtemp(prefix=token[:12] + ".incomplete-", dir=self.root))
                    directory.rename(diagnostic / "cache")
                directory.mkdir(parents=True, mode=0o700)
                request = {"snapshot": snapshot, "roots": None if self.config.allowed_roots is None
                           else list(map(str, self.config.allowed_roots))}
                (directory / "request.json").write_text(json.dumps(request))
                self._status(directory, {"state": "queued", "message": "等待建立原始预览索引"})
                self.pending.add(token)
                self.executor.submit(self._build, token)
            return self.status(token)

    @staticmethod
    def _status(directory, value):
        temporary = directory / "status.tmp"
        temporary.write_text(json.dumps(value, ensure_ascii=False))
        temporary.replace(directory / "status.json")

    def _build(self, token):
        directory = self.root / token
        try:
            with self.lock:
                if self.closed:
                    return
                self._status(directory, {"state": "running", "message": "正在读取原始录制"})
                with (directory / "worker.log").open("w") as logfile:
                    process = subprocess.Popen([str(self.config.python), "-u", "-m", "data_web_platform.raw_worker",
                        str(directory / "request.json")], cwd=self.config.project_root,
                        env=self.config.environment(), stdout=logfile, stderr=subprocess.STDOUT)
                self.process = process
            try:
                code = process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError("原始预览索引超过 10 分钟，已停止；请选择更短的录制")
            if code or not (directory / "manifest.json").is_file():
                state = json.loads((directory / "status.json").read_text())
                if state.get("state") != "failed":
                    raise RuntimeError(f"原始预览子进程退出（{code}）；请查看服务日志")
        except Exception as error:
            self._status(directory, {"state": "failed", "message": str(error)})
        finally:
            with self.lock:
                self.pending.discard(token)
                self.process = None

    def directory(self, token):
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise HTTPException(404, "预览不存在")
        directory = self.root / token
        if not directory.is_dir():
            raise HTTPException(404, "预览不存在")
        return directory

    def status(self, token):
        directory = self.directory(token)
        if (directory / "manifest.json").is_file():
            manifest = self.manifest(token)
            return {"id": token, "state": "ready", "manifest": manifest}
        status = json.loads((directory / "status.json").read_text())
        if token not in self.pending and status.get("state") in ("queued", "running"):
            status = {"state": "interrupted", "message": "索引过程已中断，请重新打开该录制"}
        return {"id": token, **status}

    def manifest(self, token):
        from .raw_data import source_snapshot
        directory = self.directory(token)
        path = directory / "manifest.json"
        if not path.is_file():
            raise HTTPException(409, "原始预览尚未准备完成")
        manifest = json.loads(path.read_text())
        # Check freshness on every data/media request, not only on first open.
        if source_snapshot(manifest["path"], self.config.allowed_roots)["id"] != token:
            raise HTTPException(409, "源文件已变化，请重新打开录制以建立新索引")
        return manifest

    def close(self):
        with self.lock:
            self.closed = True
            process = self.process
            if process and process.poll() is None:
                process.terminate()
        if process and process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.executor.shutdown(wait=True, cancel_futures=True)


def create_raw_router(service):
    router = APIRouter(prefix="/api/raw")

    @router.post("/open", status_code=202)
    def open_recording(request: RawRequest):
        return service.open(request.path)

    @router.get("/{token}")
    def status(token: str):
        return service.status(token)

    @router.get("/{token}/series")
    def get_series(token: str, topic: str, fields: list[str] = Query(...), clock: str = "record",
                   start: float = 0, end: float | None = None, limit: int = Query(1800, ge=100, le=5000)):
        from .raw_data import series
        manifest = service.manifest(token)
        if clock not in ("record", "header"):
            raise ValueError("未知时间轴")
        end = manifest["clocks"][clock]["duration"] if end is None else end
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError("时间范围无效")
        return series(service.directory(token), manifest, topic, fields, clock, start, end, limit)

    @router.get("/{token}/samples")
    def get_samples(token: str, topics: list[str] = Query(...), clock: str = "record", at: float = 0):
        from .raw_data import samples
        manifest = service.manifest(token)
        if clock not in ("record", "header") or not math.isfinite(at) or at < 0 or not 1 <= len(topics) <= 32:
            raise ValueError("时间或 topic 选择无效")
        return samples(service.directory(token), manifest, topics, clock, at)

    @router.get("/{token}/video/{clock}")
    def video(token: str, clock: str):
        manifest = service.manifest(token)
        if clock not in ("record", "header") or not manifest["video"].get(clock):
            raise HTTPException(404, "该时间轴的视频不可用；请查看预览说明")
        return FileResponse(service.directory(token) / f"quad-{clock}.mp4", media_type="video/mp4")

    return router

"""Full upstream viewer behind /viewer, with a registry-scoped local data API.

Only the platform's private cache contains frontend adaptations.  The source
checkout is never modified.  SSR reads use a token-protected loopback bridge;
browser reads use same-origin routes and never receive the internal token.
"""
from __future__ import annotations

import asyncio
import bisect
import contextlib
import fcntl
import hashlib
import hmac
import importlib.util
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from .paths import validate_path
from .publishing import new_staging, publish_noreplace

INTERNAL_HEADER = "X-DWP-Internal-Token"
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailer", "transfer-encoding", "upgrade"}
_backend_modules: dict[str, Any] = {}
_backend_lock = threading.Lock()
# These optional, root-level chart files are supported by the pinned upstream
# viewer. This is an exact filename allowlist, not access to the dataset root.
OPTIONAL_PROGRESS_FILES = ("sarm_progress.parquet", "srm_progress.parquet")
_ANNOTATION_FILE = "lerobot_annotations.json"


def _record(registry, identifier: str) -> dict:
    try:
        value = registry.get(identifier)
    except (KeyError, ValueError):
        value = None
    if not value:
        raise HTTPException(404, "Dataset is not registered")
    return value


def _root(config, record: dict) -> Path:
    return _validated_path(config, record["path"])


def _validated_path(config, path, *, must_exist=True) -> Path:
    try:
        return validate_path(path, config.allowed_roots, must_exist=must_exist)
    except ValueError:
        raise HTTPException(403, "无效或不可访问的服务器路径") from None


def _inside(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    """Reject traversal and symlink escapes, including in dataset metadata."""
    rel = PurePosixPath(relative)
    if (not relative or rel.is_absolute() or "\\" in relative or
            any(p in {"..", "."} or p.startswith(".") for p in rel.parts)):
        raise HTTPException(403, "Invalid dataset-relative path")
    path = root.joinpath(*rel.parts).resolve(strict=False)
    if not path.is_relative_to(root):
        raise HTTPException(403, "Dataset path escapes its registered root")
    if must_exist and not path.is_file():
        raise HTTPException(404, "Dataset file not found")
    return path


@contextlib.contextmanager
def _annotation_directory(root: Path):
    """Pin the real meta directory; annotation writes never follow symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = meta_fd = None
    try:
        try:
            root_fd = os.open(root, flags)
            meta_fd = os.open("meta", flags, dir_fd=root_fd)
        except OSError:
            raise HTTPException(403, "Annotation writes require a real dataset/meta directory, not a symlink") from None
        yield meta_fd
    finally:
        if meta_fd is not None:
            os.close(meta_fd)
        if root_fd is not None:
            os.close(root_fd)


def _check_annotation_target(meta_fd: int) -> None:
    try:
        existing = os.stat(_ANNOTATION_FILE, dir_fd=meta_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(existing.st_mode):
        raise HTTPException(403, "Annotation writes refuse symlinks and non-regular annotation files")


def dataset_file(config, registry, repo_id: str, relative: str) -> Path:
    # Never turn this into a general filesystem browser.  URDF uploads remain
    # browser-local; only LeRobot's published metadata/data/media are served.
    if (relative not in OPTIONAL_PROGRESS_FILES
            and PurePosixPath(relative).parts[:1] not in [("meta",), ("data",), ("videos",)]):
        raise HTTPException(403, "Only dataset metadata, data and videos are served")
    return _inside(_root(config, _record(registry, repo_id)), relative)


def _episode_metadata_shards(root: Path) -> list[str]:
    """Enumerate only real, canonical local v3 episode metadata files.

    Do not follow directory/file symlinks during discovery or trust paths from
    info.json. Listing is two levels deep, not an unrestricted filesystem walk.
    """
    episodes = root / "meta" / "episodes"
    if not episodes.resolve().is_relative_to(root) or not episodes.is_dir():
        return []
    found = []
    try:
        with os.scandir(episodes) as chunks:
            for chunk in chunks:
                match = re.fullmatch(r"chunk-([0-9]+)", chunk.name)
                if not match or not chunk.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(chunk.path) as files:
                    for entry in files:
                        file_match = re.fullmatch(r"file-([0-9]+)\.parquet", entry.name)
                        if not file_match or not entry.is_file(follow_symlinks=False):
                            continue
                        relative = f"meta/episodes/{chunk.name}/{entry.name}"
                        try:
                            _inside(root, relative)
                        except HTTPException:
                            continue
                        found.append((int(match[1]), int(file_match[1]), relative))
    except OSError:
        raise HTTPException(422, "Cannot enumerate local episode metadata") from None
    return [relative for _, _, relative in sorted(found)]


def viewer_dataset_info(config, registry, repo_id: str) -> dict:
    """Advertise existing files without changing on-disk metadata.

    The browser and SSR bridge receive the same manifest, so the viewer never
    probes absent files. Do not trust a manifest supplied by a dataset itself.
    """
    path = dataset_file(config, registry, repo_id, "meta/info.json")
    try:
        info = json.loads(path.read_text())
        if not isinstance(info, dict):
            raise ValueError("Dataset metadata must be an object")
    except (ValueError, OSError):
        raise HTTPException(422, "Invalid dataset info.json") from None
    available = []
    root = _root(config, _record(registry, repo_id))
    for name in OPTIONAL_PROGRESS_FILES:
        try:
            _inside(root, name)
        except HTTPException:
            continue  # Missing files and unsafe symlinks are not advertised.
        available.append(name)
    info["_dwp_optional_files"] = available
    info["_dwp_episode_shards"] = _episode_metadata_shards(root) if info.get("codebase_version") == "v3.0" else []
    return info


def _byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if not header:
        return 0, max(-1, size - 1), False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()) or size == 0:
        raise ValueError("Unsatisfiable range")
    first, last = match.groups()
    if first:
        start, end = int(first), int(last) if last else size - 1
    else:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Unsatisfiable range")
        start, end = max(0, size - suffix), size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable range")
    return start, min(end, size - 1), True


class DataBridge:
    """No browser access/CORS.  Only our Next server knows the bearer secret."""
    def __init__(self, config, registry):
        self.config, self.registry = config, registry
        self.server = None
        self.thread = None

    def start(self) -> str:
        if self.server:
            return self.origin
        owner = self
        token = self.config.internal_token
        if not token:
            raise RuntimeError("Initialize Config before starting the viewer")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                # compare_digest prevents timing leakage; never log the header.
                if not hmac.compare_digest(self.headers.get(INTERNAL_HEADER, ""), token):
                    self.send_error(403)
                    return
                match = re.fullmatch(r"/datasets/([^/]+)/([^/]+)/resolve/main/(.+)",
                                     unquote(urlsplit(self.path).path))
                if not match:
                    self.send_error(404)
                    return
                try:
                    path = dataset_file(owner.config, owner.registry,
                                        f"{match[1]}/{match[2]}", match[3])
                    # Keep SSR's metadata identical to the same-origin
                    # browser route; the optional-files manifest is virtual.
                    body = (json.dumps(viewer_dataset_info(owner.config, owner.registry,
                                                          f"{match[1]}/{match[2]}"),
                                       ensure_ascii=False).encode()
                            if match[3] == "meta/info.json" else None)
                    size = len(body) if body is not None else path.stat().st_size
                    start, end, partial = _byte_range(self.headers.get("Range"), size)
                except HTTPException as exc:
                    self.send_error(exc.status_code)
                    return
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                except OSError:
                    self.send_error(404)
                    return
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                try:
                    if body is not None:
                        self.wfile.write(body[start:end + 1])
                        return
                    with path.open("rb") as stream:
                        stream.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            block = stream.read(min(1024 * 1024, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, name="viewer-data-bridge", daemon=True)
        self.thread.start()
        return self.origin

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = None


class ViewerService:
    """Own exactly one loopback Next process, without starting builds on requests.

    Call start/stop from the FastAPI lifespan (both are async).  A missing build
    is reported through status and /viewer returns 503, while the rest of the
    platform remains usable.  Build with ``python setup_viewer.py`` first.
    """
    def __init__(self, config, registry):
        self.config, self.registry = config, registry
        self.bridge = DataBridge(config, registry)
        self.process = None
        self.log = None
        self.client: httpx.AsyncClient | None = None
        self.error: str | None = None
        self.ready = False
        self.source = config.state_dir / "viewer-cache/source"

    async def start(self):
        if self.client is not None:
            return
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.config.viewer_port}",
                                        follow_redirects=False, trust_env=False,
                                        timeout=httpx.Timeout(90, connect=3))
        if not (self.source / ".next/BUILD_ID").is_file():
            self.error = "Viewer is not built; run python setup_viewer.py in the platform checkout"
            return
        if not Path(self.config.bun).is_file():
            self.error = "Configured Bun executable is missing"
            return
        # Never attach to an unknown listener that could serve unrelated data.
        import socket
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", self.config.viewer_port)) == 0:
                self.error = f"Viewer port {self.config.viewer_port} is already occupied"
                return
        try:
            origin = self.bridge.start()
            env = self.config.environment()
            env.update({"DWP_DATA_ORIGIN": origin, "NEXT_TELEMETRY_DISABLED": "1",
                        "NEXT_PUBLIC_ANNOTATE_BACKEND_URL": "/",
                        "NEXT_PUBLIC_LEROBOT_DOCTOR_URL": "/doctor"})
            self.log = (self.config.state_dir / "viewer.log").open("ab", buffering=0)
            self.process = subprocess.Popen(
                [str(self.config.bun), "run", "start", "--hostname", "127.0.0.1", "--port", str(self.config.viewer_port)],
                cwd=self.source, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Viewer process exited; inspect the private viewer.log")
                try:
                    response = await self.client.get("/viewer")
                    if response.status_code < 500:
                        self.ready = True
                        self.error = None
                        return
                except httpx.RequestError:
                    pass
                await asyncio.sleep(.25)
            raise RuntimeError("Viewer did not become ready within 45 seconds")
        except Exception as exc:
            self.error = str(exc)
            await self._stop_process()

    async def _stop_process(self):
        if self.process and self.process.poll() is None:
            import signal
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(self.process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await asyncio.to_thread(self.process.wait, timeout=5)
        self.process = None
        if self.log:
            self.log.close()
            self.log = None
        await asyncio.to_thread(self.bridge.stop)
        self.ready = False

    async def stop(self):
        await self._stop_process()
        if self.client:
            await self.client.aclose()
            self.client = None

    def status(self):
        return {"ready": self.ready and (self.process is None or self.process.poll() is None),
                "error": self.error, "url": "/viewer", "port": self.config.viewer_port}


class DatasetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_id: str | None = None
    local_path: str | None = None
    revision: str | None = None


class LanguageAtom(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    role: str = Field(min_length=1, max_length=100)
    content: str | None = Field(default=None, max_length=100000)
    style: str | None = None
    timestamp: float = Field(ge=0)
    camera: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class AtomsPayload(DatasetRef):
    episode_index: int = Field(ge=0)
    atoms: list[LanguageAtom] = Field(default_factory=list, max_length=100000)


class ExportRequest(DatasetRef):
    output_dir: str | None = None
    copy_videos: bool = False


class HubRequest(DatasetRef):
    hf_token: str = Field(min_length=1, max_length=4096, repr=False)
    push_in_place: bool = True
    new_repo_id: str | None = None
    private: bool = False
    commit_message: str = Field(default="Add language annotations", max_length=4096)


def _backend(config):
    """Reuse the pinned upstream language schema/materializer, not its server.

    Its global HF download/cache and recursive output replacement are purposely
    never invoked.  Local v2.1 support and safe transactions live in this module.
    """
    source = config.letools_root / "third_party/external/lerobot-dataset-visualizer/backend/app.py"
    if not source.is_file():
        from letools.external import visualizer_source
        source = visualizer_source() / "backend/app.py"
    key = str(source.resolve())
    with _backend_lock:
        if key not in _backend_modules:
            name = "_dwp_annotations_" + hashlib.sha256(key.encode()).hexdigest()[:16]
            spec = importlib.util.spec_from_file_location(name, source)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            _backend_modules[key] = module
        return _backend_modules[key]


class AnnotationStore:
    def __init__(self, config, registry):
        self.config, self.registry = config, registry
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def resolve(self, ref: DatasetRef) -> dict:
        if ref.revision not in (None, "main"):
            raise HTTPException(400, "Local datasets have no alternate revision")
        record = _record(self.registry, ref.repo_id) if ref.repo_id else None
        if ref.local_path:
            candidate = _validated_path(self.config, ref.local_path)
            matches = [r for r in self.registry.all() if Path(r["path"]).resolve() == candidate]
            if not matches:
                raise HTTPException(404, "Register the local dataset before annotating it")
            if record and _root(self.config, record) != candidate:
                raise HTTPException(400, "repo_id and local_path refer to different datasets")
            record = record or matches[0]
        if not record:
            raise HTTPException(400, "repo_id or a registered local_path is required")
        return record

    @contextlib.contextmanager
    def locked(self, root: Path):
        key = str(root)
        with self._guard:
            lock = self._locks.setdefault(key, threading.RLock())
        # Also coordinate CLI/platform workers using this state directory.
        directory = self.config.state_dir / "annotation-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock, (directory / (hashlib.sha256(key.encode()).hexdigest() + ".lock")).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self, record):
        import pandas as pd
        root = _root(self.config, record)
        backend = _backend(self.config)
        info = json.loads(_inside(root, "meta/info.json").read_text())
        version = str(info.get("codebase_version", ""))
        if version.startswith("v2"):
            rows = [json.loads(line) for line in _inside(root, "meta/episodes.jsonl").read_text().splitlines() if line.strip()]
            episodes = pd.DataFrame(rows)
        elif version.startswith("v3"):
            files = sorted((root / "meta/episodes").rglob("*.parquet"))
            if not files:
                raise HTTPException(422, "No episode metadata parquet files")
            episodes = pd.concat([pd.read_parquet(_inside(root, p.relative_to(root).as_posix())) for p in files], ignore_index=True)
        else:
            raise HTTPException(422, "Annotations support LeRobot v2.1 and v3.x datasets")
        if "episode_index" not in episodes.columns:
            raise HTTPException(422, "Episode metadata has no episode_index")
        state = backend.DatasetState(repo_id=record["repo_id"], local_path=str(root), revision="main",
                                     root=root, info=info, episodes_df=episodes)
        annotation_file = _inside(root, "meta/lerobot_annotations.json", must_exist=False)
        if annotation_file.exists():
            # Do not silently lose an existing file on malformed JSON.
            raw = json.loads(annotation_file.read_text())
            if not isinstance(raw, dict) or not isinstance(raw.get("episodes", {}), dict):
                raise HTTPException(422, "Malformed saved annotations; refusing to overwrite")
            backend._load_existing_annotations(state)
        return state

    def episode_path(self, state, episode: int) -> Path:
        rows = state.episodes_df[state.episodes_df["episode_index"] == episode]
        if rows.empty:
            raise HTTPException(404, "Episode does not exist")
        row = rows.iloc[0]
        if str(state.info["codebase_version"]).startswith("v2"):
            template = state.info.get("data_path") or "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
            relative = template.format(episode_index=episode, episode_chunk=episode // int(state.info.get("chunks_size", 1000)))
        else:
            template = state.info.get("data_path") or "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
            relative = template.format(chunk_index=int(row["data/chunk_index"]), file_index=int(row["data/file_index"]))
        if not relative.startswith("data/"):
            raise HTTPException(422, "Invalid data_path metadata")
        return _inside(state.root, relative)

    def timestamps(self, state, episode: int) -> list[float]:
        import pyarrow.parquet as pq
        table = pq.read_table(self.episode_path(state, episode), columns=["episode_index", "timestamp"])
        values = sorted(float(ts) for ep, ts in zip(table["episode_index"].to_pylist(), table["timestamp"].to_pylist()) if ep == episode)
        if not values or not all(math.isfinite(v) and v >= 0 for v in values):
            raise HTTPException(422, "Episode has no usable finite frame timestamps")
        return values

    def atoms(self, state, episode: int) -> list[dict]:
        import pyarrow.parquet as pq
        path = self.episode_path(state, episode)
        if episode in state.annotations:
            return state.annotations[episode].atoms
        columns = [c for c in pq.read_schema(path).names if c in
                   {"episode_index", "timestamp", "language_persistent", "language_events"}]
        return _backend(self.config)._extract_existing_atoms_from_table(pq.read_table(path, columns=columns), episode)

    def save(self, state, episode: int, atoms: list[dict]) -> dict:
        backend = _backend(self.config)
        frame_ts = self.timestamps(state, episode)
        features = state.info.get("features", {})
        for atom in atoms:
            backend._validate_atom(atom)
            camera = atom.get("camera")
            if camera and (camera not in features or features[camera].get("dtype") not in {"video", "image"}):
                raise HTTPException(400, "Annotation camera is not a dataset image feature")
            if atom["timestamp"] > frame_ts[-1] + 1e-6:
                raise HTTPException(400, "Annotation timestamp exceeds the episode duration")
            if backend.column_for_style(atom.get("style")) == backend.LANGUAGE_EVENTS:
                index = bisect.bisect_left(frame_ts, atom["timestamp"])
                neighbors = frame_ts[max(0, index - 1):min(len(frame_ts), index + 1)]
                atom["timestamp"] = min(neighbors, key=lambda value: abs(value - atom["timestamp"]))
        state.annotations[episode] = backend.EpisodeAnnotations(atoms=atoms)
        payload = {"version": 2, "schema": {"persistent_styles": sorted(backend.PERSISTENT_STYLES),
                                          "event_styles": sorted(backend.EVENT_ONLY_STYLES)},
                   "episodes": {str(ep): {"atoms": ann.atoms} for ep, ann in state.annotations.items()}}
        path = state.root / "meta" / _ANNOTATION_FILE
        with _annotation_directory(state.root) as meta_fd:
            _check_annotation_target(meta_fd)
            temporary = None
            try:
                name = ".annotations-" + secrets.token_hex(16) + ".tmp"
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=meta_fd)
                temporary = name
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Check again after writing. Even a last-instant symlink swap
                # cannot redirect rename: both names are relative to this fd,
                # and replacing a directory entry never follows its referent.
                _check_annotation_target(meta_fd)
                os.replace(temporary, _ANNOTATION_FILE, src_dir_fd=meta_fd, dst_dir_fd=meta_fd)
                temporary = None
                os.fsync(meta_fd)
            finally:
                if temporary is not None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=meta_fd)
        return {"ok": True, "saved": len(atoms), "path": str(path)}

    def export(self, state, output: str | None, copy_videos: bool) -> dict:
        import pyarrow.parquet as pq
        backend = _backend(self.config)
        if output is None:
            output = str(self.config.state_dir / "exports" / (state.root.name + "-annotated-" + secrets.token_hex(5)))
        destination = _validated_path(self.config, output, must_exist=False)
        # Never replace, nest inside, or encompass an input/registered dataset.
        for record in self.registry.all():
            existing = _root(self.config, record)
            if destination == existing or destination.is_relative_to(existing) or existing.is_relative_to(destination):
                raise HTTPException(400, "Export destination overlaps a registered dataset")
        if destination.exists() or destination.is_symlink():
            raise HTTPException(409, "Export destination already exists; choose a new directory")
        try:
            stage = new_staging(destination)
        except FileExistsError:
            raise HTTPException(409, "Export destination already exists; choose a new directory") from None
        persistent = events = 0
        try:
            stage.mkdir()
            # Explicit file walks forbid symlink escapes before copying any data.
            for folder in ("meta", "videos"):
                for source in sorted((state.root / folder).rglob("*")):
                    if not source.is_file():
                        continue
                    relative = source.relative_to(state.root).as_posix()
                    if any(p.startswith(".") for p in PurePosixPath(relative).parts):
                        continue
                    source = _inside(state.root, relative)
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if folder == "videos" and not copy_videos:
                        try:
                            os.link(source, target)
                            continue
                        except OSError:
                            pass
                    shutil.copy2(source, target)
            data_files = sorted((state.root / "data").rglob("*.parquet"))
            if not data_files:
                raise HTTPException(422, "Dataset has no data parquet files")
            for source in data_files:
                relative = source.relative_to(state.root).as_posix()
                source = _inside(state.root, relative)
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                table, count_p, count_e = backend._materialize_table(pq.read_table(source), {ep: ann.atoms for ep, ann in state.annotations.items()})
                pq.write_table(table, target)
                persistent += count_p
                events += count_e
            info = json.loads(json.dumps(state.info))
            features = info.setdefault("features", {})
            features.pop("subtask_index", None)
            features.pop("tools", None)
            for name in (backend.LANGUAGE_PERSISTENT, backend.LANGUAGE_EVENTS):
                features[name] = {"dtype": "language", "shape": [1], "names": None}
            tools = info.get("tools") or []
            if not any(isinstance(t, dict) and (t.get("function") or {}).get("name") == "say" for t in tools):
                info["tools"] = [*tools, backend.SAY_TOOL_SCHEMA]
            (stage / "meta/info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
            for legacy in ("subtasks.parquet", "tasks_high_level.parquet"):
                (stage / "meta" / legacy).unlink(missing_ok=True)
            # Export paths share a lock independently of the source dataset.
            with self.locked(destination):
                try:
                    publish_noreplace(stage, destination)
                except FileExistsError:
                    raise HTTPException(409, "Export destination was created by another operation") from None
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            with contextlib.suppress(OSError):
                stage.parent.rmdir()
            raise
        return {"output_dir": str(destination), "persistent_rows": persistent, "event_rows": events}


def create_viewer_router(config, registry, *, service: ViewerService | None = None) -> APIRouter:
    """Mount this router once, under the platform's same-origin request checks."""
    router = APIRouter()
    viewer = service or ViewerService(config, registry)
    store = AnnotationStore(config, registry)
    # Expose the owner for callers that prefer router.viewer_service.start().
    router.viewer_service = viewer

    @router.api_route("/datasets/{org}/{alias}/resolve/main/{path:path}", methods=["GET", "HEAD"])
    def serve_dataset(org: str, alias: str, path: str, request: Request):
        if path == "meta/info.json":
            response = JSONResponse(viewer_dataset_info(config, registry, f"{org}/{alias}"),
                                    headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
            if request.method == "HEAD":
                response.body = b""  # Preserve the GET Content-Length.
            return response
        return FileResponse(dataset_file(config, registry, f"{org}/{alias}", path),
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.get("/api/viewer/status")
    def viewer_status():
        return viewer.status()

    @router.get("/api/health")
    def annotations_health():
        backend = _backend(config)
        return {"ok": True, "service": "platform-local-annotations", "versions": ["v2.1", "v3.0"],
                "persistent_styles": sorted(backend.PERSISTENT_STYLES), "event_styles": sorted(backend.EVENT_ONLY_STYLES)}

    @router.post("/api/dataset/load")
    def load_dataset(ref: DatasetRef):
        record = store.resolve(ref)
        with store.locked(_root(config, record)):
            state = store.load(record)
            backend = _backend(config)
            return {"ok": True, "repo_id": state.repo_id, "local_path": state.local_path,
                    "revision": "main", "root": str(state.root), "fps": float(state.info.get("fps", 30)),
                    "num_episodes": int(state.episodes_df["episode_index"].nunique()),
                    "persistent_styles": sorted(backend.PERSISTENT_STYLES), "event_styles": sorted(backend.EVENT_ONLY_STYLES)}

    @router.get("/api/episodes/{episode_index}/atoms")
    def get_atoms(episode_index: int, repo_id: str | None = None, local_path: str | None = None, revision: str | None = None):
        record = store.resolve(DatasetRef(repo_id=repo_id, local_path=local_path, revision=revision))
        with store.locked(_root(config, record)):
            return {"episode_index": episode_index, "atoms": store.atoms(store.load(record), episode_index)}

    @router.post("/api/episodes/{episode_index}/atoms")
    def set_atoms(episode_index: int, payload: AtomsPayload):
        if episode_index != payload.episode_index:
            raise HTTPException(400, "Episode index mismatch")
        record = store.resolve(payload)
        with store.locked(_root(config, record)):
            return store.save(store.load(record), episode_index, [atom.model_dump() for atom in payload.atoms])

    @router.get("/api/episodes/{episode_index}/frame_timestamps")
    def get_timestamps(episode_index: int, repo_id: str | None = None, local_path: str | None = None, revision: str | None = None):
        record = store.resolve(DatasetRef(repo_id=repo_id, local_path=local_path, revision=revision))
        with store.locked(_root(config, record)):
            return {"episode_index": episode_index, "timestamps": store.timestamps(store.load(record), episode_index)}

    @router.post("/api/export")
    def export_dataset(payload: ExportRequest):
        record = store.resolve(payload)
        with store.locked(_root(config, record)):
            return store.export(store.load(record), payload.output_dir, payload.copy_videos)

    @router.post("/api/push_to_hub")
    def push_to_hub(payload: HubRequest):
        # This endpoint is the *only* Hub write.  The original UI's explicit
        # push dialog must supply a new namespace/repository and HF token.
        if payload.push_in_place or not payload.new_repo_id:
            raise HTTPException(400, "Local datasets require an explicit new Hub repository; in-place push is disabled")
        target = payload.new_repo_id
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", target) or target.startswith("local/"):
            raise HTTPException(400, "Invalid target Hub repository")
        record = store.resolve(payload)
        with store.locked(_root(config, record)):
            result = store.export(store.load(record), None, True)
        from huggingface_hub import HfApi
        api = HfApi(token=payload.hf_token)
        # exist_ok=False deliberately prevents an accidental overwrite of an
        # existing repo.  Download/export do not invoke any Hub write methods.
        try:
            api.create_repo(target, repo_type="dataset", private=payload.private, exist_ok=False)
            api.upload_folder(folder_path=result["output_dir"], repo_id=target, repo_type="dataset", commit_message=payload.commit_message)
        except Exception:
            raise HTTPException(502, "Hub upload failed. The local export is preserved; inspect the Hub repository before retrying.") from None
        return {"ok": True, "repo_id": target, "url": f"https://huggingface.co/datasets/{target}",
                "message": f"Pushed annotated dataset to {target}"}

    async def proxy(request: Request):
        if viewer.client is None or not viewer.status()["ready"]:
            raise HTTPException(503, viewer.error or "Viewer is not running")
        # Preserve raw path and query for Next RSC routes and HTTP range fetches.
        raw_path = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
        target = raw_path + (("?" + request.url.query) if request.url.query else "")
        connection_headers = {value.strip().lower() for value in request.headers.get("connection", "").split(",")}
        excluded = _HOP_HEADERS | connection_headers | {"host", "content-length", "accept-encoding", INTERNAL_HEADER.lower()}
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in excluded]
        headers.extend([("x-forwarded-host", request.headers.get("host", "")), ("x-forwarded-proto", request.url.scheme),
                        ("accept-encoding", "identity")])
        upstream_request = viewer.client.build_request(request.method, target, headers=headers, content=request.stream())
        try:
            response = await viewer.client.send(upstream_request, stream=True)
        except httpx.RequestError:
            raise HTTPException(502, "Viewer is temporarily unavailable") from None
        result = StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                                   background=BackgroundTask(response.aclose))
        # Keep duplicate Set-Cookie fields; never comma-join them.
        result.raw_headers = [(k, v) for k, v in response.headers.raw if k.decode().lower() not in _HOP_HEADERS]
        return result

    router.add_api_route("/viewer", proxy, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    router.add_api_route("/viewer/{path:path}", proxy, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    return router

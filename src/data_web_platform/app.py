from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from .config import Config
from .datasets import DatasetRegistry
from .doctor import create_doctor_router
from .jobs import JobManager
from .models import JobRequest
from .paths import browse_directory, validate_path, validate_output_path
from .viewer import ViewerService, create_viewer_router


def discover_providers(config):
    result = subprocess.run(
        [str(config.python), "-m", "letools.cli", "providers", "list"],
        env=config.environment(), capture_output=True, text=True, timeout=25, check=True)
    providers = json.loads(result.stdout)
    for provider in providers:
        provider["web_supported"] = provider["name"] == "tianji"
        provider["description"] = ("Tianji MCAP · 严格检查 / 时间对齐 / H.264 高画质" if provider["name"] == "tianji"
                                    else "LeTools 内置 provider；当前 Web 转换面板尚未适配")
    return providers


def list_recordings(path, config):
    root = validate_path(path, config.allowed_roots)
    if not root.is_dir():
        raise ValueError("原始数据集必须是目录")
    def recognizable(candidate):
        return (candidate.name.startswith("my_bag-") or (candidate / "data").exists()
                or (candidate / "video").exists()) and not (candidate / "meta/info.json").exists()

    if (root / "tianji-cache.json").exists():
        raise ValueError("请选择原始 Tianji 录制目录，不是已 prepare 的缓存")
    candidates = [root] if recognizable(root) else sorted(root.iterdir())
    records = []
    for candidate in candidates:
        if candidate.name.startswith("."):
            continue
        try:
            child = validate_path(candidate, config.allowed_roots)
            if child.is_dir() and recognizable(child):
                metadata = validate_path(child / "data/metadata.yaml", config.allowed_roots, must_exist=False)
                records.append({"name": child.name, "path": str(child),
                                "incomplete": not metadata.is_file()})
        except (ValueError, OSError):
            continue
    return {"root": str(root), "recordings": records}


def prepare_job(payload: JobRequest, config, providers):
    available = {p["name"] for p in providers if p["web_supported"]}
    if payload.provider not in available:
        raise ValueError("该 provider 没有安装在配置的 LeTools 环境中，或没有对应的 Web 参数适配器")
    listing = list_recordings(payload.source_root, config)
    allowed = {p["path"] for p in listing["recordings"]}
    selected = [str(validate_path(p, config.allowed_roots)) for p in payload.recordings]
    if len(set(selected)) != len(selected):
        raise ValueError("不能重复选择同一条录制")
    if not set(selected).issubset(allowed):
        raise ValueError("选择中存在不属于当前原始数据集目录的录制")
    request = payload.model_dump()
    request.update(source_root=listing["root"], recordings=selected)
    if payload.kind == "convert":
        if not payload.destination:
            raise ValueError("转换必须指定新输出目录")
        request["destination"] = str(validate_output_path(
            payload.destination, config.allowed_roots, [listing["root"], *selected]))
    else:
        request["destination"] = None
    return request


class DatasetPath(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


def create_app(config=None, *, start_services=True):
    config = config or Config.from_env()
    config.initialize()
    registry = DatasetRegistry(config)
    jobs = JobManager(config, registry)
    viewer = ViewerService(config, registry)
    providers = []
    environment_error = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal providers, environment_error
        try:
            providers = await run_in_threadpool(discover_providers, config)
        except Exception as error:
            environment_error = f"配置的 LeTools 环境无法加载: {error}"
        if start_services:
            jobs.start()
            await viewer.start()
        yield
        if start_services:
            await run_in_threadpool(jobs.close)
            await viewer.stop()

    app = FastAPI(title="Data Web Platform", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.config, app.state.registry, app.state.jobs, app.state.viewer = config, registry, jobs, viewer
    app.state.providers = lambda: providers

    @app.middleware("http")
    async def protect_requests(request: Request, call_next):
        path = request.url.path
        # Trusted-network access needs no login, but other sites must not turn
        # a browser visiting the platform into a cross-site write proxy.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            expected = urlsplit(str(request.url))
            actual = urlsplit(origin)
            if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if path in {"/", "/login"} or (path.startswith("/api/") and not path.startswith("/api/proxy")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def bad_input(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def permission_error(request, error):
        return JSONResponse({"detail": f"服务账号没有访问权限: {error.filename}"}, status_code=403)

    @app.exception_handler(FileNotFoundError)
    async def missing(request, error):
        return JSONResponse({"detail": f"文件或目录不存在: {error.filename}"}, status_code=404)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "data-web-platform"}

    @app.get("/api/environment")
    def environment():
        return {"letools_root": str(config.letools_root), "python": str(config.python),
                "providers": providers, "roots": [str(p) for p in config.allowed_roots if p.is_dir()],
                "default_source": config.default_source,
                "default_output": str(Path.home() / "datasets"),
                "error": environment_error, "viewer": viewer.status(),
                "policy": "strict; no source deletion/splitting; new outputs only"}

    @app.get("/api/browse")
    def browse(path: str):
        result = browse_directory(path, config.allowed_roots)
        result.setdefault("directories", result.get("dirs", []))
        return result

    @app.get("/api/recordings")
    def recordings(path: str, provider: str = "tianji"):
        if provider != "tianji":
            raise HTTPException(400, "当前录制选择面板支持 tianji provider")
        return list_recordings(path, config)

    @app.post("/api/jobs", status_code=202)
    def submit(payload: JobRequest):
        if environment_error:
            raise HTTPException(503, environment_error)
        return jobs.submit(prepare_job(payload, config, providers))

    @app.get("/api/jobs")
    def list_jobs():
        return jobs.all()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return jobs.get(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在")

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        try:
            return jobs.cancel(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在")

    @app.get("/api/jobs/{job_id}/report")
    def get_report(job_id: str):
        result = get_job(job_id)
        return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="{result["id"]}-report.json"'})

    @app.get("/api/datasets")
    def datasets():
        return registry.all()

    @app.post("/api/datasets", status_code=201)
    def register(payload: DatasetPath):
        return registry.register(payload.path)

    app.include_router(create_doctor_router(config, registry, jobs))
    app.include_router(create_viewer_router(config, registry, service=viewer))
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/login")
    def login_page():
        # Keep old bookmarks and already-open login tabs usable after upgrade.
        return RedirectResponse("/", status_code=303)

    return app


def main():
    import uvicorn
    config = Config.from_env()
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1,
                proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()

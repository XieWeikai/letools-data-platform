"""Structured upstream Doctor interface, with copy-only mutation requests.

Integration: create_doctor_router(config, registry, jobs) calls
jobs.submit_doctor(prepared_request). The prepared JSON is written by JobManager
to its job directory and passed to ``python -m data_web_platform.doctor_worker
request.json``. The worker emits the standard DWP_EVENT job events.
"""
from __future__ import annotations

import html
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from starlette.concurrency import run_in_threadpool

from .datasets import dataset_fingerprint
from .paths import validate_output_path, validate_path
from .explanations import annotate_report

CHECKS = ("metadata", "temporal", "actions", "videos", "statistics", "episodes",
          "consistency", "training", "anomalies", "portability", "per_episode", "kinematics")
FIXES = ("reindex", "timestamps", "nan", "metadata", "episodes")
OPERATIONS = ("check", "score", "gate", "merge-check", "fix", "trim")


class DoctorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    operation: Literal["check", "score", "gate", "merge-check", "fix", "trim"] = "check"
    dataset: str | None = None
    datasets: list[str] = Field(default_factory=list, max_length=100)
    checks: list[str] | None = None
    max_episodes: int | None = Field(default=None, ge=1)
    urdf: str | None = None
    fail_on: Literal["warn", "fail"] = "fail"
    fixes: list[str] | None = None
    dry_run: StrictBool = False
    output: str | None = None
    confirm: StrictBool = False
    threshold: float = Field(default=.01, ge=0)
    min_active: int = Field(default=10, ge=1)
    keep_static: StrictBool = False
    drop_threshold: float = Field(default=30, ge=0, le=100)
    policy: Literal["act", "diffusion", "smolvla", "pi0"] = "act"
    chunk_size: int | None = Field(default=None, ge=1)
    post_merge: StrictBool = False

    @model_validator(mode="after")
    def validate_operation(self):
        for names, allowed, label in ((self.checks, CHECKS, "checks"), (self.fixes, FIXES, "fixes")):
            if names is not None and (not names or not set(names) <= set(allowed)):
                raise ValueError(f"{label} 必须是非空的已知选项列表")
        if self.operation == "merge-check":
            if len(self.datasets) < (1 if self.post_merge else 2):
                raise ValueError("merge-check 需至少两个数据集；post-merge 需一个")
            if self.post_merge and len(self.datasets) != 1:
                raise ValueError("post-merge 只能检查一个合并后的数据集")
            if len(self.datasets) != len(set(self.datasets)):
                raise ValueError("merge-check 数据集不能重复")
        elif not self.dataset:
            raise ValueError("必须指定已注册的数据集 ID")
        if self.operation in {"fix", "trim"} and (not self.confirm or not self.output):
            raise ValueError("fix/trim 必须明确确认，并填写全新的输出目录（只修改副本）")
        return self


def prepare_doctor_request(payload: dict | DoctorRequest, registry, config) -> dict:
    request = payload if isinstance(payload, DoctorRequest) else DoctorRequest.model_validate(payload)
    identities = request.datasets if request.operation == "merge-check" else [request.dataset]
    records = [registry.get(identity, deep=True) for identity in identities]
    sources = [str(validate_path(record["path"], config.allowed_roots)) for record in records]
    if len(set(sources)) != len(sources):
        raise ValueError("merge-check 不能重复引用同一个数据集")
    output = None
    if request.operation in {"fix", "trim"}:
        output = str(validate_output_path(request.output, config.allowed_roots, sources))
    options = request.model_dump(exclude={"operation", "dataset", "datasets", "output", "confirm"})
    if request.urdf:
        urdf = validate_path(request.urdf, config.allowed_roots)
        if not urdf.is_file():
            raise ValueError("URDF 必须是本地文件")
        options["urdf"] = str(urdf)
    return {"kind": "doctor", "operation": request.operation, "dataset": records[0]["repo_id"],
            "sources": sources, "output": output,
            "destination": None if request.operation == "trim" and request.dry_run else output,
            "confirm": request.confirm,
            "allowed_roots": None if config.allowed_roots is None else [str(root) for root in config.allowed_roots],
            "options": options}


def build_doctor_command(payload: dict | DoctorRequest, registry, config) -> list[str]:
    """An argv vector only; no shell, executable, flags or code from the client."""
    prepared = prepare_doctor_request(payload, registry, config)
    return [str(config.python), "-m", "data_web_platform.doctor_worker", "--request-json",
            json.dumps(prepared, ensure_ascii=False)]


def run_check(path: Path, *, checks=None, max_episodes=None, urdf=None, workspace=None) -> dict:
    from lerobot_doctor.dataset_loader import load_local
    from lerobot_doctor.report import report_to_json
    from lerobot_doctor.runner import CheckResult, Severity, run_checks
    from .doctor_compat import ACTION_CHECKS, ALIAS_MESSAGE, SOURCE_ACTION, action_dataset_view, action_view
    dataset = load_local(path, max_episodes=max_episodes)
    if dataset.info is None:
        raise ValueError(f"Doctor 无法加载数据集: {dataset.info_error}")
    if urdf:
        dataset.robot_urdf = Path(urdf)
    note = None
    requested = list(checks) if checks else list(CHECKS)
    if ACTION_CHECKS.intersection(requested):
        with action_view(path, workspace=workspace) as (view, note):
            if note:
                alias = action_dataset_view(dataset, view)
                missing = [episode.episode_index for episode in dataset.episodes_data
                           if SOURCE_ACTION not in episode.columns]
                report = None
                for name in requested:
                    # Run unchanged upstream algorithms/thresholds with the
                    # correct command name. All non-action checks use the
                    # original root, features and observations (including
                    # nested wrench windows); do not hide or flatten them.
                    target = alias if name in ACTION_CHECKS else dataset
                    if missing and name in ACTION_CHECKS:
                        # Without command data the upstream fallback scans all
                        # observations as actions. Report this broken contract
                        # explicitly rather than diagnosing unrelated sensors.
                        item = CheckResult(name=name, severity=Severity.FAIL)
                        for index in missing:
                            item.fail(f"Episode {index}: declared observation.action is missing from data")
                        from lerobot_doctor.runner import DiagnosticReport
                        partial = DiagnosticReport(dataset_path=str(path), results=[item],
                            codebase_version=dataset.info.codebase_version,
                            format_version=dataset.info.format_version,
                            total_episodes=dataset.info.total_episodes,
                            total_frames=dataset.info.total_frames, fps=dataset.info.fps)
                    else:
                        partial = run_checks(target, checks=[name])
                    if name in ACTION_CHECKS:
                        for item in partial.results:
                            item.pass_(ALIAS_MESSAGE)
                    if report is None:
                        report = replace(partial, dataset_path=dataset.display_path or str(path),
                                         dataset_name=path.name, results=[])
                    report.results.extend(partial.results)
                note = {**note, "applied_checks": [name for name in requested if name in ACTION_CHECKS]}
            else:
                report = run_checks(dataset, checks=checks)
    else:
        report = run_checks(dataset, checks=checks)
    result = json.loads(report_to_json(report))
    if note:
        result["compatibility"] = note
    return annotate_report(result)


class DoctorReports:
    """Small in-memory report cache, invalidated on dataset tree changes."""

    def __init__(self, registry, workspace=None):
        self.registry = registry
        self.workspace = workspace
        self._cache: dict[str, tuple[str, dict]] = {}
        self._lock = threading.RLock()

    def get(self, identity: str, refresh: bool = False) -> dict:
        with self._lock:
            record = self.registry.get(identity, deep=True)
            path = Path(record["path"])
            before = dataset_fingerprint(path)
            cached = self._cache.get(record["id"])
            if not refresh and cached and cached[0] == before:
                return {**cached[1], "cached": True}
            report = run_check(path, checks=list(CHECKS), workspace=self.workspace)
            after = dataset_fingerprint(path)
            if before != after:
                raise ValueError("诊断期间数据集发生变化，报告未缓存；请在录制/转换完成后重试")
            result = {**report, "repo_id": record["repo_id"], "fingerprint": after, "cached": False}
            if len(self._cache) >= 16:
                self._cache.pop(next(iter(self._cache)))
            self._cache[record["id"]] = (after, result)
            return result


def create_doctor_router(config, registry, jobs=None) -> APIRouter:
    router = APIRouter()
    reports = DoctorReports(registry, workspace=config.state_dir / "doctor-projections")

    @router.get("/api/doctor/options")
    def options():
        return {"operations": OPERATIONS, "checks": CHECKS, "fixes": FIXES,
                "policies": ["act", "diffusion", "smolvla", "pi0"],
                "mutation_policy": "copy-to-new-output-only; explicit confirmation required",
                "trim": {"implementation": "synchronized-letools", "versions": ["v2.1", "v3.0"],
                         "video": "frame-accurate H.264 CRF 18; original resolution",
                         "dry_run": "no output created", "motion": "maximum component delta across state/action",
                         "strict_alignment": True}}

    @router.get("/doctor/report.json")
    @router.get("/api/doctor/report")
    async def report(dataset: str, refresh: bool = False):
        try:
            return await run_in_threadpool(reports.get, dataset, refresh)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (OSError, ValueError) as error:
            raise HTTPException(400, str(error)) from error

    @router.post("/api/doctor/jobs", status_code=202)
    async def submit(payload: DoctorRequest):
        if jobs is None:
            raise HTTPException(503, "Doctor job runner 未配置")
        try:
            prepared = await run_in_threadpool(prepare_doctor_request, payload, registry, config)
            return jobs.submit_doctor(prepared)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (OSError, ValueError) as error:
            raise HTTPException(400, str(error)) from error

    @router.get("/doctor", response_class=HTMLResponse)
    def doctor_page(dataset: str = ""):
        return HTMLResponse(_doctor_html(dataset))

    return router


def _doctor_html(dataset: str) -> str:
    checks = "".join(f'<label><input type="checkbox" name="checks" value="{name}" checked>{name}</label>' for name in CHECKS)
    fixes = "".join(f'<label><input type="checkbox" name="fixes" value="{name}" checked>{name}</label>' for name in FIXES)
    return '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LeRobot Doctor</title><style>body{font:16px system-ui;background:#101724;color:#e8edf8;max-width:1000px;margin:32px auto;padding:0 20px}input,select,button,textarea{font:inherit;padding:8px;background:#18243a;color:inherit;border:1px solid #53637d;border-radius:5px}label{display:inline-block;margin:6px}fieldset{margin:16px 0;border:1px solid #53637d}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:16px;background:#172136}button{cursor:pointer}a{color:#93c5fd}.warning{color:#fde68a}</style>
<link rel="stylesheet" href="/static/help.css?v=0.4.0"><script src="/static/help.js?v=0.4.0" defer></script><body data-help-context="doctor">
<a href="/">← 工作台</a><h1>LeRobot Doctor</h1><p>本地 v2.1 / v3.0 数据集诊断。检查、评分、训练门禁和合并兼容性均不修改数据。参数旁的问号支持悬浮、键盘聚焦和点击查看说明。</p>
<form id="doctor"><label>数据集 <input name="dataset" style="width:34em;max-width:80vw" value="''' + html.escape(dataset, quote=True) + '''" required></label>
<label>操作 <select name="operation"><option>check</option><option>score</option><option>gate</option><option>merge-check</option><option>fix</option><option>trim</option></select></label>
<fieldset><legend>Check：12 项原生检查</legend>''' + checks + '''<br><label>最多 episodes（空=全部）<input type="number" min="1" name="max_episodes"></label><label>URDF（可选本地路径）<input name="urdf"></label><label>失败门槛<select name="fail_on"><option>fail</option><option>warn</option></select></label></fieldset>
<fieldset><legend>Score / Gate</legend><label>评分丢弃阈值<input name="drop_threshold" type="number" min="0" max="100" step="any" value="30"></label><label>策略<select name="policy"><option>act</option><option>diffusion</option><option>smolvla</option><option>pi0</option></select></label><label>Chunk size（可选）<input name="chunk_size" type="number" min="1"></label></fieldset>
<fieldset><legend>Merge-check</legend><label>数据集 ID，每行一个<textarea name="datasets" rows="3" cols="60"></textarea></label><label><input type="checkbox" name="post_merge">检查合并后的单个数据集</label></fieldset>
<fieldset><legend>Fix / Trim：只处理新输出</legend><p class="warning">原始数据始终保留。Trim 同步重建视频、时间戳、索引和统计：原分辨率 H.264 CRF 18；按 state/action 任一分量相邻帧变化超过阈值判定活动，保留首次活动前一帧，不拆分轨迹。低于最少活动帧的轨迹默认丢弃；视频缺帧或时间轴不连续会拒绝输出。</p>''' + fixes + '''<br><label>新输出绝对路径<input name="output" style="width:30em;max-width:75vw"></label><label><input type="checkbox" name="confirm">我确认只创建新输出，不修改原数据</label><label><input type="checkbox" name="dry_run">Dry run（Trim 只预览；Fix 创建隔离副本）</label><br><label>活动阈值（原始信号单位/帧）<input name="threshold" type="number" min="0" step="any" value="0.01"></label><label>最少活动帧<input name="min_active" type="number" min="1" value="10"></label><label><input type="checkbox" name="keep_static">保留静止或活动帧不足的轨迹</label></fieldset>
<button type="submit">提交后台任务</button> <button type="button" id="report">读取完整 12 项报告</button></form><p id="status" role="status"></p><div id="doctor-explanations"></div><details open><summary>完整原始报告与结构化说明</summary><pre id="result"></pre></details>
<script>
const form=document.querySelector('#doctor'),status=document.querySelector('#status'),result=document.querySelector('#result');
form.elements.operation.onchange=()=>{form.elements.dataset.required=form.elements.operation.value!=='merge-check'};
async function responseJSON(r){const data=await r.json();if(!r.ok)throw Error(JSON.stringify(data.detail||data));return data}
form.onsubmit=async e=>{e.preventDefault();const f=new FormData(form),operation=f.get('operation');let p={operation,dataset:f.get('dataset')};
if(operation==='check'){p.checks=f.getAll('checks');p.fail_on=f.get('fail_on');if(f.get('urdf'))p.urdf=f.get('urdf')}
if(['check','score'].includes(operation)&&f.get('max_episodes'))p.max_episodes=Number(f.get('max_episodes'));
if(operation==='score')p.drop_threshold=Number(f.get('drop_threshold'));
if(operation==='gate'){p.policy=f.get('policy');if(f.get('chunk_size'))p.chunk_size=Number(f.get('chunk_size'))}
if(operation==='merge-check'){p.datasets=String(f.get('datasets')).split(/\\r?\\n/).map(s=>s.trim()).filter(Boolean);p.post_merge=f.has('post_merge')}
if(['fix','trim'].includes(operation)){p.output=f.get('output');p.confirm=f.has('confirm');p.dry_run=f.has('dry_run')}
if(operation==='fix')p.fixes=f.getAll('fixes');
if(operation==='trim'){p.threshold=Number(f.get('threshold'));p.min_active=Number(f.get('min_active'));p.keep_static=f.has('keep_static')}
try{status.textContent='正在提交…';const job=await responseJSON(await fetch('/api/doctor/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}));result.textContent=JSON.stringify(job,null,2);status.textContent='后台任务已提交；可返回工作台查看进度。';if(job.id)poll(job.id)}catch(e){status.textContent=e.message}};
async function poll(id){try{const job=await responseJSON(await fetch('/api/jobs/'+encodeURIComponent(id)));result.textContent=JSON.stringify(job,null,2);window.PlatformHelp?.renderReport(document.querySelector('#doctor-explanations'),job.result?.report||job.check_report);if(!['succeeded','failed','cancelled','interrupted'].includes(job.status))setTimeout(()=>poll(id),1500)}catch(e){status.textContent=e.message}}
document.querySelector('#report').onclick=async()=>{try{status.textContent='正在运行全部检查…';const report=await responseJSON(await fetch('/doctor/report.json?dataset='+encodeURIComponent(form.elements.dataset.value)));result.textContent=JSON.stringify(report,null,2);window.PlatformHelp?.renderReport(document.querySelector('#doctor-explanations'),report);status.textContent=report.cached?'已读取未过期缓存':'检查完成'}catch(e){status.textContent=e.message}};
</script></html>'''

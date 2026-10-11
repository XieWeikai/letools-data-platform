"""Typed web adapter for the optional LeTools editor; new outputs only."""
from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from .datasets import dataset_fingerprint, inspect_dataset
from .paths import validate_output_path

EpisodeId = Annotated[int, Field(strict=True, ge=0)]
PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow')


class VideoOptions(BaseModel):
    """Allow only the editor's supported codec options, never executable paths.

    Width/height mean an exact resize of retained MP4 cameras. Chroma formats
    impose even-dimension constraints; validate them before queueing expensive
    work so an operator can correct the form without producing partial output.
    """
    model_config = ConfigDict(extra='forbid')
    codec: Literal['libx264', 'mjpeg'] = 'libx264'
    size: tuple[Annotated[int, Field(strict=True, ge=2, le=8192)], Annotated[int, Field(strict=True, ge=2, le=8192)]] | None = None
    pixel_format: Literal['yuv420p', 'yuv422p', 'yuv444p', 'yuvj420p', 'yuvj422p', 'yuvj444p'] | None = None
    crf: Annotated[int, Field(strict=True, ge=0, le=51)] | None = None
    preset: str | None = None
    quality: Annotated[int, Field(strict=True, ge=1, le=31)] | None = None
    video_keys: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode='after')
    def compatible(self):
        if self.preset is not None and self.preset not in PRESETS:
            raise ValueError('不支持的 H.264 preset')
        if self.codec == 'mjpeg' and (self.crf is not None or self.preset is not None):
            raise ValueError('MJPEG 使用 quality；CRF/preset 仅适用于 H.264')
        if self.codec == 'libx264' and self.quality is not None:
            raise ValueError('quality 仅适用于 MJPEG')
        if self.size:
            width, height = self.size
            fmt = self.pixel_format or ('yuv420p' if self.codec == 'libx264' else 'yuvj420p')
            if ('420' in fmt and (width % 2 or height % 2)) or ('422' in fmt and width % 2):
                raise ValueError('当前像素格式要求宽度/高度为偶数')
        return self


class EditorRequest(BaseModel):
    """An edit refers to source episode IDs and a previously inspected source.

    ``source_fingerprint`` detects stale forms; apply additionally names the
    completed plan job. Neither field grants access: the registry and allowed
    filesystem roots are checked again on every request and in the worker.
    """
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    dataset: str = Field(min_length=1, max_length=256)
    output: str = Field(min_length=1, max_length=4096)
    operation: Literal['plan', 'apply'] = 'plan'
    source_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    plan_job_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    task_by_episode: dict[str, str] = Field(default_factory=dict, max_length=10000)
    delete_episodes: list[EpisodeId] = Field(default_factory=list, max_length=100000)
    remove_features: list[str] = Field(default_factory=list, max_length=256)
    video: VideoOptions | None = None
    workers: Annotated[int, Field(strict=True, ge=1, le=8)] | None = None
    codec_threads: Annotated[int, Field(strict=True, ge=1, le=8)] | None = None
    batch_rows: Annotated[int, Field(strict=True, ge=1, le=1048576)] = 65536

    @field_validator('task_by_episode')
    @classmethod
    def tasks(cls, value):
        for key, text in value.items():
            if not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                raise ValueError('任务映射键必须是原始 episode 的非负整数 ID')
            if not text.strip() or len(text) > 4096:
                raise ValueError('任务文本不能为空，最多 4096 字符')
        return value

    @model_validator(mode='after')
    def conflicts(self):
        if len(set(self.delete_episodes)) != len(self.delete_episodes):
            raise ValueError('删除的 episode ID 不能重复')
        if set(map(int, self.task_by_episode)) & set(self.delete_episodes):
            raise ValueError('不能同时删除 episode 并修改它的 task')
        if not (self.task_by_episode or self.delete_episodes or self.remove_features or self.video):
            raise ValueError('请至少选择一项编辑操作')
        if self.operation == 'apply' and not self.plan_job_id:
            raise ValueError('请先预览并完成编辑计划')
        return self


def editor_status():
    try:
        from letools_editor import EditConfig, VideoEdit, edit_dataset, plan_edit, _native  # noqa: F401
        return {'available': True, 'version': version('letools-editor'), 'error': None}
    except (ImportError, OSError) as error:
        return {'available': False, 'version': None, 'error': f'专用环境尚未准备好 letools-editor: {error}'}


def edit_config(request: EditorRequest):
    """Translate the web allowlist to the optional editor's immutable config.

    Keep the FFmpeg executable and overwrite policy server-owned. Missing
    resource settings remain None so the editor, rather than this adapter,
    chooses its bounded concurrency. Source dataset FPS/version are unchanged.
    """
    from letools_editor import EditConfig, VideoEdit
    video = request.video
    return EditConfig(
        task_by_episode={int(key): value for key, value in request.task_by_episode.items()},
        delete_episodes=frozenset(request.delete_episodes),
        remove_features=frozenset(request.remove_features),
        video=None if video is None else VideoEdit(
            codec=video.codec, size=video.size, pixel_format=video.pixel_format,
            crf=23 if video.crf is None else video.crf, preset=video.preset or 'veryfast',
            quality=2 if video.quality is None else video.quality, keys=tuple(video.video_keys)),
        workers=request.workers, codec_threads=request.codec_threads, batch_rows=request.batch_rows,
        ffmpeg='/usr/bin/ffmpeg', overwrite=False)


def safe_source(path, roots):
    """Reject symlinked datasets instead of following externally owned media."""
    source, info = inspect_dataset(path, roots)
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('Editor 暂不接受含符号链接的数据集，请使用普通文件数据集')
    return source, info


def inspect_editor(identity, registry, config, offset=0, limit=100):
    """Read metadata and one page of episode choices without transcoding media.

    Capture a source fingerprint on both sides of inspection. A stable preview
    is required before the browser can submit a plan against those episode IDs.
    """
    from letools.plugins import open_dataset
    record = registry.get(identity)
    source, info = safe_source(record['path'], config.allowed_roots)
    before = dataset_fingerprint(source)
    dataset = open_dataset(source)
    result = {'id': record['id'], 'path': str(source), 'version': info['codebase_version'],
              'fps': info['fps'], 'episodes': len(dataset.episodes), 'frames': dataset.metadata.total_frames,
              'features': dataset.metadata.features, 'tasks': dataset.metadata.tasks,
              'offset': offset, 'limit': limit, 'source_fingerprint': before,
              'episode_preview': [{'index': ep.index, 'length': ep.length, 'tasks': ep.tasks,
                                   'seconds': ep.length / dataset.metadata.fps}
                                  for ep in dataset.episodes[offset:offset + limit]],
              'sidecar_notice': '输出不复制自定义附属文件，包括已有 Annotation 标注；原数据集及标注保留。'}
    if dataset_fingerprint(source) != before:
        raise ValueError('读取期间数据集发生变化，请重新加载')
    return result


def edit_identity(payload):
    """Compare plan/apply options while excluding only the transport controls."""
    return {k: v for k, v in payload.items() if k not in {'operation', 'plan_job_id'}}


def prepare_editor_request(payload, registry, config, jobs):
    """Require a successful, identical plan before reserving an apply output.

    A completed plan for another source, changed task map, changed video option
    or earlier source fingerprint cannot authorize the current apply request.
    The worker still re-plans after queueing because inputs may change later.
    """
    request = payload if isinstance(payload, EditorRequest) else EditorRequest.model_validate(payload)
    record = registry.get(request.dataset)
    source, _ = safe_source(record['path'], config.allowed_roots)
    output = validate_output_path(request.output, config.allowed_roots, [source])
    if dataset_fingerprint(source) != request.source_fingerprint:
        raise ValueError('数据集已变化，请重新加载并预览计划')
    document = request.model_dump(mode='json')
    document.update(dataset=record['id'], output=str(output))
    if request.operation == 'apply':
        plan = jobs.get(request.plan_job_id)
        previous = plan.get('request', {})
        if (plan.get('kind') != 'editor' or plan.get('status') != 'succeeded'
                or previous.get('options', {}).get('operation') != 'plan'
                or not plan.get('result', {}).get('plan')
                or previous.get('source') != str(source)
                or edit_identity(previous.get('options', {})) != edit_identity(document)):
            raise ValueError('编辑参数或源数据与已完成计划不一致，请重新预览')
    return {'kind': 'editor', 'operation': request.operation, 'source': str(source),
            'dataset': record['id'], 'allowed_roots': [str(p) for p in config.allowed_roots],
            'options': document, 'destination': str(output) if request.operation == 'apply' else None}


def create_editor_router(config, registry, jobs):
    router = APIRouter()

    @router.get('/editor', include_in_schema=False)
    def page():
        return FileResponse(Path(__file__).parent / 'static/editor.html')

    @router.get('/api/editor/options')
    def options():
        return {**editor_status(), 'presets': PRESETS, 'versions': ['v2.1', 'v3.0'],
                'operations': ['tasks', 'delete_episodes', 'remove_features', 'transcode', 'resize'],
                'policy': 'new-output-only; same-version; preview-before-apply'}

    @router.get('/api/editor/inspect')
    async def inspect(dataset: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        try:
            return await run_in_threadpool(inspect_editor, dataset, registry, config, offset, limit)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error

    @router.post('/api/editor/jobs', status_code=202)
    async def submit(payload: EditorRequest):
        status = editor_status()
        if not status['available']:
            raise HTTPException(503, status['error'])
        try:
            request = await run_in_threadpool(prepare_editor_request, payload, registry, config, jobs)
            return jobs.submit_editor(request)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error

    return router

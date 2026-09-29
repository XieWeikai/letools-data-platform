"""Frame-accurate, copy-only LeRobot trim (v2.1 and v3.0).

Unlike the upstream Doctor's table-only mutator, this adapter rebuilds the
dataset with the LeTools backends. Each retained video frame is decoded by PTS
and re-encoded once, at its original resolution, before lossless packet remux.
Nothing writes to the input. A failed output retains an incomplete marker.
"""
from __future__ import annotations

import copy
import json
import math
import tempfile
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pyarrow as pa

from letools._stats import aggregate_episode_stats
from letools._video import apply_encoding_metadata
from letools.backends.v21 import LeRobotV21Backend
from letools.backends.v30 import LeRobotV30Backend
from letools.conversion_types import ConversionConfig, VideoEncodingConfig
from letools.model import Episode, EpisodeDataProfile, VideoSlice
from letools.plugins import DatasetSource, open_dataset
from letools.telemetry import StageRecorder
from letools.validation import validate_dataset

from .datasets import dataset_fingerprint, inspect_dataset
from .paths import validate_output_path


@dataclass(frozen=True)
class _Range:
    original: Episode
    index: int
    start: int
    stop: int
    offset: int
    active_frames: int
    signals: tuple[str, ...]

    @property
    def length(self):
        return self.stop - self.start


def _numeric(table, key):
    try:
        values = np.asarray(table[key].to_pylist(), dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key}: 需要等长数值向量") from error
    if values.ndim < 1 or values.shape[0] != table.num_rows or not np.isfinite(values).all():
        raise ValueError(f"{key}: 含空值/NaN/Inf 或非等长向量，请先运行 Doctor 检查/修复")
    return values


def _plan(source, threshold, min_active, keep_static):
    kept, details, offset = [], [], 0
    fps = source.metadata.fps
    for original in source.episodes:
        table = source.read_episode(original)
        if table.num_rows != original.length or original.length < 1:
            raise ValueError(f"episode {original.index}: 帧数与元数据不一致")
        for key in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
            if key not in table.column_names:
                raise ValueError(f"episode {original.index}: 缺少 {key}")
        timestamps = _numeric(table, "timestamp").reshape(-1)
        if not np.allclose(timestamps - timestamps[0], np.arange(original.length) / fps,
                           rtol=0, atol=max(1e-4, .01 / fps)):
            raise ValueError(f"episode {original.index}: 不是连续的 {fps} FPS 时间轴，不能安全裁剪")
        frames = _numeric(table, "frame_index").reshape(-1)
        if not np.array_equal(frames, np.arange(original.length)):
            raise ValueError(f"episode {original.index}: frame_index 不连续，请先修复")
        if not np.all(_numeric(table, "episode_index") == original.index):
            raise ValueError(f"episode {original.index}: episode_index 与元数据不一致")
        signals = tuple(key for key in table.column_names
                        if key in {"action", "observation.action", "observation.state"}
                        or key.startswith(("action.", "observation.action.", "observation.state.")))
        if not signals:
            raise ValueError("找不到 action / observation.action / observation.state，不能判定静止")
        active = np.zeros(original.length, dtype=bool)
        for key in signals:
            values = _numeric(table, key).reshape(original.length, -1)
            if not values.shape[1]:
                raise ValueError(f"{key}: 空向量不能用于判定活动")
            # Max, not a dimension-dependent mean: one moving joint must count.
            active[1:] |= np.max(np.abs(np.diff(values, axis=0)), axis=1) > threshold
        indices = np.flatnonzero(active)
        enough = len(indices) >= min_active
        if not enough and not keep_static:
            details.append({"source_episode": original.index, "dropped": True,
                            "active_frames": len(indices), "source_frames": original.length,
                            "reason": "below_min_active", "signals": list(signals)})
            continue
        # Keep the sample immediately before the first transition, so the
        # beginning of real movement is represented rather than silently lost.
        start, stop = (max(0, int(indices[0]) - 1), int(indices[-1]) + 1) if enough else (0, original.length)
        item = _Range(original, len(kept), start, stop, offset, len(indices), signals)
        kept.append(item)
        offset += item.length
        details.append({"source_episode": original.index, "output_episode": item.index,
                        "dropped": False, "source_frames": original.length,
                        "start_frame": start, "stop_frame_exclusive": stop,
                        "kept_frames": item.length, "active_frames": len(indices),
                        "signals": list(signals)})
    if not kept:
        raise ValueError("所有轨迹均低于最少活动帧条件；没有创建空数据集。可降低阈值或明确选择 keep_static。")
    return kept, details


def _table(source, item):
    table = source.read_episode(item.original).slice(item.start, item.length)
    values = {"timestamp": np.arange(item.length) / source.metadata.fps,
              "frame_index": np.arange(item.length),
              "episode_index": np.full(item.length, item.index),
              "index": np.arange(item.offset, item.offset + item.length)}
    for key, value in values.items():
        index = table.column_names.index(key)
        table = table.set_column(index, table.schema.field(index), pa.array(value, type=table[key].type))
    return table


def _stats(table, features):
    result = {}
    for key in table.column_names:
        if features.get(key, {}).get("dtype") in {"image", "video", "string"}:
            continue
        column_type = table[key].type
        while pa.types.is_list(column_type) or pa.types.is_fixed_size_list(column_type) or pa.types.is_large_list(column_type):
            column_type = column_type.value_type
        if not (pa.types.is_floating(column_type) or pa.types.is_integer(column_type) or pa.types.is_boolean(column_type)):
            continue
        values = _numeric(table, key)
        if values.ndim == 1:
            values = values[:, None]
        result[key] = {"min": values.min(axis=0).tolist(), "max": values.max(axis=0).tolist(),
                       "mean": values.mean(axis=0).tolist(), "std": values.std(axis=0).tolist(),
                       "count": [table.num_rows]}
    return result


class _TrimmedSource(DatasetSource):
    def __init__(self, source, ranges):
        self.root, self.source, self.ranges = source.root, source, ranges
        info = copy.deepcopy(source.metadata.info)
        count = sum(item.length for item in ranges)
        splits = {}
        for name, interval in source.metadata.splits.items():
            try:
                first, last = map(int, interval.split(":"))
            except (AttributeError, ValueError) as error:
                raise ValueError(f"不支持的 split 范围: {name}={interval}") from error
            splits[name] = f"{sum(item.original.index < first for item in ranges)}:{sum(item.original.index < last for item in ranges)}"
        info.update(total_frames=count, total_episodes=len(ranges), splits=splits,
                    total_tasks=len(source.metadata.tasks))
        self.metadata = replace(source.metadata, features=info["features"], total_frames=count,
                                total_episodes=len(ranges), splits=splits, info=info)
        self.episodes = tuple(Episode(index=item.index, length=item.length,
            tasks=item.original.tasks, stats=_stats(_table(source, item), self.metadata.features),
            data_path=item.original.data_path, data_start=0, data_end=item.length, videos={}) for item in ranges)

    def read_episode(self, episode):
        return _table(self.source, self.ranges[episode.index])

    def data_profile(self, episode):
        item = self.ranges[episode.index]
        original = self.source.data_profile(item.original)
        return EpisodeDataProfile(original.locality_key,
            max(1, int(original.episode_logical_bytes * item.length / item.original.length)),
            original.resource_logical_bytes, original.resource_physical_bytes, original.resource_rows)


def _encode_slice(media, item, target, fps, *, feature):
    if not isinstance(media, VideoSlice):
        raise ValueError("同步裁剪只接受标准 v2.1/v3.0 MP4 视频")
    start = media.start + item.start / fps
    tolerance = .1 / fps
    if media.end + tolerance < media.start + item.stop / fps:
        raise ValueError(f"视频元数据时段不足: {media.path}")
    totals, squares = np.zeros(3), np.zeros(3)
    minima, maxima, pixels, produced = np.full(3, 255.), np.zeros(3), 0, 0
    with av.open(str(media.path)) as decoder:
        if len(decoder.streams.video) != 1:
            raise ValueError(f"需要单视频流: {media.path}")
        stream = decoder.streams.video[0]
        stream.codec_context.thread_count = 2
        width, height = stream.width, stream.height
        if width % 2 or height % 2:
            raise ValueError("H.264 yuv420p 需要偶数宽高；不会擅自缩放或裁去像素")
        shape = feature.get("shape")
        if shape and list(shape) != [height, width, 3]:
            raise ValueError(f"视频尺寸与元数据不符: {media.path}: {width}x{height}, shape={shape}")
        if start:
            decoder.seek(max(0, int(start / stream.time_base)), stream=stream, backward=True, any_frame=False)
        with av.open(str(target), mode="w") as encoder:
            output = encoder.add_stream("libx264", rate=fps, options={"crf": "18", "preset": "fast"})
            output.width, output.height, output.pix_fmt = width, height, "yuv420p"
            output.codec_context.thread_count = 2
            output.codec_context.max_b_frames = 0
            output.codec_context.gop_size = fps
            output.time_base = Fraction(1, fps)
            for frame in decoder.decode(stream):
                if frame.pts is None:
                    raise ValueError(f"视频帧没有 PTS: {media.path}")
                timestamp = float(frame.pts * frame.time_base)
                if timestamp < start - tolerance:
                    continue
                expected = start + produced / fps
                if abs(timestamp - expected) > tolerance:
                    raise ValueError(f"视频与数据不对齐/缺帧: {media.path}, 期望 {expected:.6f}s, 实际 {timestamp:.6f}s")
                if (frame.width, frame.height) != (width, height):
                    raise ValueError("视频中途改变了分辨率")
                rgb = frame.to_ndarray(format="rgb24").reshape(-1, 3)
                totals += rgb.sum(axis=0, dtype=np.float64)
                squares += np.square(rgb, dtype=np.float64).sum(axis=0)
                minima = np.minimum(minima, rgb.min(axis=0))
                maxima = np.maximum(maxima, rgb.max(axis=0))
                pixels += len(rgb)
                frame.pts, frame.time_base = produced, Fraction(1, fps)
                frame.pict_type = av.video.frame.PictureType.I if produced == 0 else av.video.frame.PictureType.NONE
                for packet in output.encode(frame):
                    encoder.mux(packet)
                produced += 1
                if produced == item.length:
                    break
            if produced != item.length:
                raise ValueError(f"视频帧不足: {media.path}: {produced}/{item.length}")
            for packet in output.encode():
                encoder.mux(packet)
    mean = totals / pixels
    stats = {"min": (minima / 255).reshape(3, 1, 1).tolist(),
             "max": (maxima / 255).reshape(3, 1, 1).tolist(),
             "mean": (mean / 255).reshape(3, 1, 1).tolist(),
             "std": (np.sqrt(np.maximum(squares / pixels - mean**2, 0)) / 255).reshape(3, 1, 1).tolist(),
             "count": [produced]}
    return VideoSlice(target, 0., produced / fps), stats


def _verify_videos(path):
    """Decode each final shard once; count and PTS must match its episode rows."""
    source = open_dataset(path)
    expected = {}
    fps = source.metadata.fps
    for episode in source.episodes:
        for video in episode.videos.values():
            first = int(round(video.start * fps))
            if abs(video.start - first / fps) > .1 / fps:
                raise ValueError("输出视频元数据不在帧网格上")
            previous = expected.get(video.path, 0)
            if first != previous:
                raise ValueError("输出视频 episode 范围不连续")
            expected[video.path] = first + episode.length
    for video, count in expected.items():
        produced = 0
        with av.open(str(video)) as container:
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 2
            for frame in container.decode(stream):
                if frame.pts is None or abs(float(frame.pts * frame.time_base) - produced / fps) > .1 / fps:
                    raise ValueError(f"输出视频 PTS 不连续: {video}")
                produced += 1
        if produced != count:
            raise ValueError(f"输出视频帧数 {produced} != 数据帧数 {count}: {video}")


def trim_dataset(source_path, destination, roots, *, threshold=.01, min_active=10,
                 keep_static=False, dry_run=False, progress=None, defer_publication=False):
    """Plan or rebuild; threshold = maximum absolute per-component frame delta.

    State AND command signals participate (either can show activity); any gap or
    nonfinite data fails closed. The sample before the first transition is kept.
    Below-min-active episodes are dropped unless keep_static is explicit.
    Dry-run creates no directory and performs no video encoding.
    The worker can defer marker removal until its outer validation/source checks
    finish too. This option is internal, not accepted from web client payloads.
    """
    if not math.isfinite(threshold) or threshold < 0 or min_active < 1:
        raise ValueError("无效的活动阈值/最少活动帧")
    source_path, _ = inspect_dataset(source_path, roots)
    destination = validate_output_path(destination, roots, [source_path])
    if any(path.is_symlink() for path in source_path.rglob("*")):
        raise ValueError("裁剪暂不接受含符号链接的数据集，请提供普通文件副本")
    before = dataset_fingerprint(source_path)
    source = open_dataset(source_path)
    if source.metadata.fps < 1 or float(source.metadata.info["fps"]) != source.metadata.fps:
        raise ValueError("FPS 必须为正整数")
    ranges, details = _plan(source, threshold, min_active, keep_static)
    trimmed = _TrimmedSource(source, ranges)
    report = {"implementation": "synchronized-letools", "dry_run": dry_run,
              "source_version": source.metadata.version, "fps": source.metadata.fps,
              "source_episodes": len(source.episodes), "kept_episodes": len(ranges),
              "dropped_episodes": len(source.episodes) - len(ranges),
              "source_frames": source.metadata.total_frames, "kept_frames": trimmed.metadata.total_frames,
              "removed_frames": source.metadata.total_frames - trimmed.metadata.total_frames,
              "details": details, "motion_rule": "max absolute component delta across state/action > threshold",
              "threshold": threshold, "min_active": min_active, "keep_static": keep_static,
              "video_validation": "not_run_in_dry_run" if dry_run else "decoded_frame_count_and_pts",
              "video": {"codec": "h264", "crf": 18, "preset": "fast", "resolution": "unchanged"}}
    if dry_run:
        if before != dataset_fingerprint(source_path):
            raise ValueError("预览期间源数据发生变化")
        return report
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = validate_output_path(destination, roots, [source_path])
    destination.mkdir(mode=0o700, exist_ok=False)
    marker = destination / ".dwp-incomplete"
    marker.write_text("Synchronized trim has not completed validation.\n")
    encoding = VideoEncodingConfig(codec="libx264", pixel_format="yuv420p", codec_threads=2)
    config = ConversionConfig(workers=2, video_workers=2, video_encoding=encoding)
    with tempfile.TemporaryDirectory(prefix=".trim-work-", dir=destination) as temporary:
        for item, episode in zip(ranges, trimmed.episodes):
            if progress:
                progress(item.index, len(ranges))
            for key_index, key in enumerate(trimmed.metadata.video_keys):
                target = Path(temporary) / f"episode-{item.index:06d}-camera-{key_index:02d}.mp4"
                video, stats = _encode_slice(source.media_input(item.original, key), item, target,
                                            source.metadata.fps, feature=trimmed.metadata.features[key])
                episode.videos[key], episode.stats[key] = video, stats
                apply_encoding_metadata(trimmed.metadata.features[key], source.metadata.fps,
                    encoding, include_legacy_video_info=source.metadata.version == "v2.1", output=target)
        backend = LeRobotV21Backend() if source.metadata.version == "v2.1" else LeRobotV30Backend()
        # Direct backend avoids convert()'s same-version prohibition and avoids
        # its overwrite/rename logic. Only our exclusively reserved dir is used.
        backend.write(trimmed, destination, config, StageRecorder())
        (destination / "meta/stats.json").write_text(json.dumps(
            aggregate_episode_stats([episode.stats for episode in trimmed.episodes]), ensure_ascii=False))
    validation = validate_dataset(destination, deep=True)
    if not validation.valid:
        raise ValueError("裁剪后的数据集验证失败: " + "; ".join(validation.errors))
    _verify_videos(destination)
    if before != dataset_fingerprint(source_path):
        raise ValueError("裁剪期间源数据发生变化；未发布输出")
    (destination / "meta/trim_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not defer_publication:
        marker.unlink()
    report["path"] = str(destination)
    return report

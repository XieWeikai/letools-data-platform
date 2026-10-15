"""Read-only MCAP inspection, independent of conversion acceptance and sampling.

The cache retains raw numeric fields and both clocks. Plot reduction selects
actual extrema; it never averages, interpolates, normalizes or fills commands.
Original H.264 access units are remuxed once, with their recorded timestamps.
"""
from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct

import numpy as np
import yaml

from .paths import validate_path

FORMAT = "raw-mcap-viewer-v1"
VIDEO_TOPIC = "/recorder/quad_tile/compressed_undistorted"
CAMERAS = [dict(name=name, label=label, column=x, row=y) for name, label, x, y in (
    ("head_left", "头部 · 左", 0, 0), ("head_right", "头部 · 右", 1, 0),
    ("left_wrist", "左腕", 0, 1), ("right_wrist", "右腕", 1, 1))]
MAX_FIELDS = 256
MAX_MESSAGES = 2_000_000
MAX_VALUES = 20_000_000


def source_snapshot(path, roots):
    """Resolve every manifest path before a worker reads it, including symlinks."""
    root = validate_path(path, roots)
    if not root.is_dir() or (root / "meta/info.json").exists() or (root / "tianji-cache.json").exists():
        raise ValueError("请选择一条原始 MCAP 录制，而不是 LeRobot 数据集或准备缓存")
    manifests = []
    if (root / "data/metadata.yaml").is_file():
        manifests.append(root / "data/metadata.yaml")
    if (root / "video").is_dir():
        manifests.extend(sorted((root / "video").rglob("metadata.yaml")))
    if not manifests:
        raise ValueError("录制中未找到 data/video 的 metadata.yaml")
    files, topics, inputs = [], {}, set()
    for manifest in manifests:
        resolved = validate_path(manifest, roots)
        if not resolved.is_relative_to(root):
            raise ValueError("录制元数据不能通过符号链接指向外部")
        info = yaml.safe_load(resolved.read_text())["rosbag2_bagfile_information"]
        inputs.add(resolved)
        for item in info.get("topics_with_message_count", []):
            meta = item["topic_metadata"]
            topics.setdefault(meta["name"], {"type": meta.get("type", "unknown"), "declared_count": 0})
            topics[meta["name"]]["declared_count"] += int(item.get("message_count", 0))
        for name in info.get("relative_file_paths", []):
            p = validate_path(resolved.parent / name, roots)
            if not p.is_relative_to(resolved.parent) or p.suffix != ".mcap" or not p.is_file():
                raise ValueError("MCAP 清单路径必须位于对应录制目录内")
            if str(p) not in files:
                files.append(str(p))
                inputs.add(p)
    for name in (".recording-session.json", "session.json"):
        p = root / name
        if p.exists():
            p = validate_path(p, roots)
            if not p.is_relative_to(root):
                raise ValueError("录制状态文件不能指向外部")
            state = json.loads(p.read_text())
            if state.get("phase") not in (None, "saved", "closed") or any(
                    state.get(k) in ("recording", "starting", "stopping", "running") for k in ("data_state", "video_state")):
                raise ValueError("录制尚未保存关闭，请结束录制后再查看")
            inputs.add(p)
    if not files or len(files) > 128:
        raise ValueError("单条预览需要 1–128 个 MCAP 文件")
    fingerprint = []
    for p in sorted(inputs):
        s = p.stat()
        fingerprint.append([str(p), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns])
    key = hashlib.sha256(json.dumps([FORMAT, str(root), fingerprint]).encode()).hexdigest()
    return {"id": key, "path": str(root), "files": files, "topics": topics, "fingerprint": fingerprint}


def plain(value, depth=0):
    """Bound JSON display without discarding an entire unfamiliar topic."""
    if depth > 8:
        return "[嵌套超过 8 层]"
    if isinstance(value, np.integer):
        return int(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value if not isinstance(value, str) or len(value) <= 65536 else value[:65536] + "…[截断]"
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else str(value)
    if isinstance(value, (Sequence, np.ndarray, bytes, bytearray)):
        result = [plain(v, depth + 1) for v in value[:MAX_FIELDS]]
        if len(value) > MAX_FIELDS:
            result.append(f"[仅显示前 {MAX_FIELDS} 项，共 {len(value)} 项]")
        return result
    if isinstance(value, dict):
        return {str(k): plain(v, depth + 1) for k, v in value.items() if not str(k).startswith("_")}
    names = getattr(value, "__slots__", ()) or vars(value).keys()
    return {name: plain(getattr(value, name), depth + 1) for name in names if not name.startswith("_")}


def numeric_fields(value, prefix=""):
    result = {}
    def visit(item, path):
        if path == "header" or len(result) >= MAX_FIELDS:
            return
        if isinstance(item, (int, float)):
            result[path or "value"] = float(item)
        elif isinstance(item, str) and item in ("nan", "inf", "-inf"):
            result[path] = float(item)
        elif isinstance(item, dict):
            for name, child in item.items():
                visit(child, f"{path}.{name}" if path else name)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
    visit(value, prefix)
    return result


def header_time(message):
    try:
        stamp = message.header.stamp
        sec, ns = int(stamp.sec), int(stamp.nanosec)
        value = sec * 1_000_000_000 + ns
        return value if 0 < value <= np.iinfo(np.int64).max and 0 <= ns < 1_000_000_000 else -1
    except (AttributeError, TypeError, ValueError):
        return -1


def compressed_image(record, schema, channel):
    if schema is None or schema.name != "sensor_msgs/msg/CompressedImage" or channel.message_encoding != "cdr":
        raise ValueError("四宫格需要 sensor_msgs/CompressedImage CDR")
    declarations = [" ".join(line.split("#", 1)[0].split()).replace("/msg/", "/")
                    for line in schema.data.decode().split("====", 1)[0].splitlines()]
    if [v for v in declarations if v] != ["std_msgs/Header header", "string format", "uint8[] data"]:
        raise ValueError("四宫格 CompressedImage schema 不是标准布局")
    raw = memoryview(record.data)
    if bytes(raw[:2]) not in (b"\x00\x01", b"\x00\x00"):
        raise ValueError("视频只支持 CDR v1")
    endian = "<" if raw[1] else ">"
    sec, ns, size = struct.unpack_from(endian + "iII", raw, 4)
    pos = (16 + size + 3) & ~3
    size = struct.unpack_from(endian + "I", raw, pos)[0]
    fmt = bytes(raw[pos + 4:pos + 4 + size - 1]).decode()
    pos = (pos + 4 + size + 3) & ~3
    length = struct.unpack_from(endian + "I", raw, pos)[0]
    if pos + 4 + length != len(raw) or "h264" not in fmt.lower():
        raise ValueError("四宫格不是有效 H.264 payload")
    stamp = sec * 1_000_000_000 + ns
    stamp = stamp if stamp > 0 and 0 <= ns < 1_000_000_000 else -1
    return stamp, raw[pos + 4:], fmt


def clock_info(times, origin):
    delta = np.diff(times)
    positive = delta[delta > 0]
    median = float(np.median(positive)) if len(positive) else 0
    return {"start": (int(times.min()) - origin) / 1e9 if len(times) else None,
            "end": (int(times.max()) - origin) / 1e9 if len(times) else None,
            "hz": 1e9 / median if median else None, "max_gap_ms": float(delta.max()) / 1e6 if len(delta) else 0,
            "gap_threshold": max(.1, median / 1e9 * 5),
            "reversals": int(np.sum(delta < 0)), "duplicates": int(np.sum(delta == 0))}


def remux(h264, target, times):
    """Preserve compressed packets; variable PTS follows actual MCAP timestamps."""
    import av
    if len(times) < 1 or np.any(np.diff(times) <= 0):
        raise ValueError("视频时间戳重复或回退：无法生成可靠播放时间轴，原始 topic 仍可查看")
    relative = times - times[0]
    with av.open(str(h264), format="h264") as source:
        template = source.streams.video[0]
        decoder = template.codec_context
        decoder.thread_count = 1
        if decoder.has_b_frames:
            raise ValueError("原始视频含 B 帧，当前查看器不猜测解码/显示顺序")
        width, height = decoder.width, decoder.height
        if not width or not height or width % 2 or height % 2:
            raise ValueError("四宫格视频尺寸无效")
        count, decoded = 0, 0
        with av.open(str(target), "w", format="mp4", options={"movflags": "+faststart"}) as output:
            stream = output.add_stream_from_template(template)
            stream.time_base = Fraction(1, 1_000_000_000)
            for packet in source.demux(template):
                if not packet.size:
                    continue
                if count >= len(times):
                    raise ValueError("H.264 access unit 数量多于 MCAP 消息")
                # Check original one-access-unit/one-frame contract without
                # changing source payload or converting pixels.
                packet.pts = packet.dts = count
                packet.time_base = Fraction(1, 1_000_000_000)
                for frame in decoder.decode(packet):
                    if frame.pts != decoded or (frame.width, frame.height) != (width, height):
                        raise ValueError("原始视频帧序或尺寸发生变化")
                    decoded += 1
                packet.pts = packet.dts = int(relative[count])
                packet.duration = int(relative[count + 1] - relative[count]) if count + 1 < len(times) else (
                    int(np.median(np.diff(times))) if len(times) > 1 else 33_333_333)
                packet.stream = stream
                output.mux(packet)
                count += 1
            for frame in decoder.decode(None):
                if frame.pts != decoded:
                    raise ValueError("原始视频尾帧顺序不明确")
                decoded += 1
        if count != len(times) or decoded != count:
            raise ValueError(f"视频消息/帧数量不一致: {len(times)}/{count}/{decoded}")
    return {"width": width, "height": height, "frames": count}


def build_cache(snapshot, destination, progress=lambda **kw: None):
    from mcap.reader import make_reader
    from tianji_provider.decoder import CompiledDecoderFactory

    destination = Path(destination)
    topics = {name: {"name": name, "type": meta["type"], "declared_count": meta["declared_count"],
                     "errors": [], "rows": [], "fields": set(), "observed_count": 0}
              for name, meta in snapshot["topics"].items()}
    connection = sqlite3.connect(destination / "messages.sqlite")
    connection.execute("CREATE TABLE messages(topic TEXT, seq INTEGER, record_ns INTEGER, header_ns INTEGER, payload TEXT)")
    video_raw = destination / "original-quad.h264"
    count, value_count = 0, 0
    try:
        with video_raw.open("xb") as video_handle:
            for filename in snapshot["files"]:
                with Path(filename).open("rb") as handle:
                    reader = make_reader(handle)
                    factory = CompiledDecoderFactory()
                    for schema, channel, record in reader.iter_messages(log_time_order=False):
                        topic = topics.setdefault(channel.topic, {"name": channel.topic, "type": schema.name if schema else "unknown",
                            "declared_count": 0, "errors": [], "rows": [], "fields": set(), "observed_count": 0})
                        topic["observed_count"] += 1
                        count += 1
                        if count > MAX_MESSAGES or value_count > MAX_VALUES:
                            raise ValueError("单条预览超过 200 万消息或 2000 万数值的资源限制，请选择更短的录制")
                        if record.log_time <= 0 or record.log_time > np.iinfo(np.int64).max:
                            topic["errors"].append("MCAP 时间戳无效")
                            continue
                        try:
                            if channel.topic == VIDEO_TOPIC:
                                stamp, payload, fmt = compressed_image(record, schema, channel)
                                video_handle.write(payload)
                                message = {"format": fmt, "payload_bytes": len(payload)}
                            else:
                                decoder = factory.decoder_for(channel.message_encoding, schema)
                                if decoder is None:
                                    raise ValueError(f"不支持该 schema/编码: {channel.message_encoding}")
                                decoded = decoder(record.data)
                                stamp, message = header_time(decoded), plain(decoded)
                            numeric = numeric_fields(message)
                            new_fields = topic["fields"] | numeric.keys()
                            if len(new_fields) > MAX_FIELDS:
                                raise ValueError(f"数值字段超过 {MAX_FIELDS}，无法绘制该消息")
                            payload_json = json.dumps(message, ensure_ascii=False, allow_nan=False)
                        except Exception as error:
                            detail = f"{type(error).__name__}: {error}"
                            if detail not in topic["errors"]:
                                topic["errors"].append(detail[:1200])
                            continue
                        topic["fields"] = new_fields
                        topic["rows"].append((int(record.log_time), stamp, count, numeric))
                        value_count += len(numeric)
                        connection.execute("INSERT INTO messages VALUES (?,?,?,?,?)", (
                            channel.topic, count, int(record.log_time), stamp if stamp > 0 else int(record.log_time), payload_json))
                        if count % 5000 == 0:
                            connection.commit()
                            progress(stage="messages", messages=count, message="读取原始消息与字段")
        connection.execute("CREATE INDEX by_record ON messages(topic, record_ns, seq)")
        connection.execute("CREATE INDEX by_header ON messages(topic, header_ns, seq)")
        connection.commit()
    finally:
        connection.close()
    all_rows = [t for t in topics.values() if t["rows"]]
    if not all_rows:
        raise ValueError("未能读取任何有效消息：" + "; ".join(e for t in topics.values() for e in t["errors"])[:2000])
    origins = {"record": min(r[0] for t in all_rows for r in t["rows"]),
               "header": min(r[1] if r[1] > 0 else r[0] for t in all_rows for r in t["rows"])}
    ends = {"record": max(r[0] for t in all_rows for r in t["rows"]),
            "header": max(r[1] if r[1] > 0 else r[0] for t in all_rows for r in t["rows"])}
    result = {"format": FORMAT, "id": snapshot["id"], "path": snapshot["path"], "name": Path(snapshot["path"]).name,
              "fingerprint": snapshot["fingerprint"], "messages": count,
              "clocks": {key: {"origin_ns": str(value), "duration": (ends[key] - value) / 1e9} for key, value in origins.items()},
              "topics": [], "video": {"available": False, "errors": []}, "cameras": CAMERAS}
    for index, topic in enumerate(topics.values()):
        rows = topic.pop("rows")
        import re
        fields = sorted(topic.pop("fields"), key=lambda key: [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", key)])
        times = np.asarray([r[0] for r in rows], dtype=np.int64)
        headers = np.asarray([r[1] for r in rows], dtype=np.int64)
        header_times = np.where(headers > 0, headers, times)
        values = np.asarray([[r[3].get(f, math.nan) for f in fields] for r in rows], dtype=np.float64).reshape(len(rows), len(fields))
        token = f"topic-{index:03d}"
        np.savez(destination / f"{token}.npz", record=times, header=header_times, values=values)
        offset = (headers[headers > 0] - times[headers > 0]) / 1e6
        topic.update(id=token, fields=fields, count=len(rows), header_count=int(np.sum(headers > 0)),
                     invalid_values=int(np.sum(~np.isfinite(values))),
                     header_offset_ms={"min": float(offset.min()), "max": float(offset.max())} if len(offset) else None,
                     clocks={"record": clock_info(times, origins["record"]), "header": clock_info(header_times, origins["header"])})
        result["topics"].append(topic)
        if topic["name"] == VIDEO_TOPIC and rows:
            progress(stage="video", messages=count, message="封装原始视频（不重新编码）")
            for clock, stamps in (("record", times), ("header", header_times)):
                try:
                    if topic["errors"]:
                        raise ValueError("部分视频消息无法读取，拒绝生成有误导性的连续预览")
                    target = destination / f"quad-{clock}.mp4"
                    if clock == "header" and np.array_equal(times, header_times) and result["video"].get("record"):
                        target.hardlink_to(destination / "quad-record.mp4")
                        meta = {k: result["video"]["record"][k] for k in ("width", "height", "frames")}
                    else:
                        meta = remux(video_raw, target, stamps)
                    result["video"][clock] = {**meta, "offset": (int(stamps[0]) - origins[clock]) / 1e9,
                        "end": (int(stamps[-1]) - origins[clock]) / 1e9,
                        "times": ((stamps - origins[clock]) / 1e9).tolist(), "file": target.name}
                    result["video"]["available"] = True
                except Exception as error:
                    result["video"]["errors"].append(f"{clock}: {type(error).__name__}: {error}")
    # The elementary stream is an owned transient, not an original recording.
    video_raw.unlink()
    return result


def select_points(times, values, start, end, limit):
    """Min/max envelope using actual samples; preserve gaps and NaN boundaries."""
    order = np.argsort(times, kind="stable")
    times, values = times[order], values[order]
    left, right = np.searchsorted(times, [start, end], side="left")
    left, right = max(0, left - 1), min(len(times), right + 1)
    times, values = times[left:right], values[left:right]
    if len(times) <= limit:
        return times, values, False
    indices = {0, len(times) - 1}
    buckets = max(1, limit // max(4, values.shape[1] * 2 + 2))
    for chunk in np.array_split(np.arange(len(times)), buckets):
        indices.update((int(chunk[0]), int(chunk[-1])))
        for column in values.T:
            finite = chunk[np.isfinite(column[chunk])]
            if len(finite):
                indices.update((int(finite[np.argmin(column[finite])]), int(finite[np.argmax(column[finite])])))
    # Keep discontinuity endpoints even when dense plot reduction is enabled.
    delta = np.diff(times)
    positive = delta[delta > 0]
    threshold = max(.1, float(np.median(positive)) * 5 if len(positive) else .1)
    edges = np.flatnonzero((delta > threshold) | np.any(~np.isfinite(values[:-1]) | ~np.isfinite(values[1:]), axis=1))
    if len(edges) > limit:
        raise ValueError("当前区间的缺口或非有限值过多，请放大时间轴；不会省略缺口后画连续曲线")
    for index in edges:
        indices.update((int(index), int(index + 1)))
    selected = sorted(indices)
    return times[selected], values[selected], True


def series(cache, manifest, topic_id, fields, clock, start, end, limit):
    topic = next((t for t in manifest["topics"] if t["id"] == topic_id), None)
    if topic is None:
        raise ValueError("未知 topic")
    if not fields or len(fields) > 16 or any(f not in topic["fields"] for f in fields):
        raise ValueError("每张图请选择 1–16 个有效数值字段")
    origin = int(manifest["clocks"][clock]["origin_ns"])
    with np.load(cache / f"{topic_id}.npz", allow_pickle=False) as data:
        times = (data[clock] - origin) / 1e9
        values = data["values"][:, [topic["fields"].index(f) for f in fields]]
    raw_times = np.sort(times, kind="stable")
    gap_prefix = np.r_[0, np.cumsum(np.diff(raw_times) > topic["clocks"][clock]["gap_threshold"])]
    times, values, reduced = select_points(times, values, start, end, limit)
    indices = np.searchsorted(raw_times, times, side="left")
    breaks = np.r_[True, np.diff(gap_prefix[indices]) > 0].tolist() if len(times) else []
    return {"topic": topic_id, "fields": fields, "times": times.tolist(),
            "values": [[float(v) if math.isfinite(v) else None for v in row] for row in values],
            "reduced": reduced, "gap_threshold": topic["clocks"][clock]["gap_threshold"],
            "count": topic["count"], "clock": clock, "breaks": breaks}


def samples(cache, manifest, topics, clock, at):
    origin = int(manifest["clocks"][clock]["origin_ns"])
    timestamp = origin + round(at * 1e9)
    column = "record_ns" if clock == "record" else "header_ns"
    result = []
    with sqlite3.connect(f"file:{cache / 'messages.sqlite'}?mode=ro", uri=True) as connection:
        for token in topics:
            topic = next((t for t in manifest["topics"] if t["id"] == token), None)
            if topic is None:
                raise ValueError("未知 topic")
            row = connection.execute(f"SELECT {column},record_ns,header_ns,payload FROM messages "
                f"WHERE topic=? AND {column}<=? ORDER BY {column} DESC,seq DESC LIMIT 1", (topic["name"], timestamp)).fetchone()
            if row is None:
                result.append({"id": token, "sample": None})
            else:
                result.append({"id": token, "time": (row[0] - origin) / 1e9, "age_ms": (timestamp - row[0]) / 1e6,
                    "record_ns": str(row[1]), "header_or_record_ns": str(row[2]), "sample": json.loads(row[3]),
                    "outside": at > (topic["clocks"][clock]["end"] or 0)})
    return result

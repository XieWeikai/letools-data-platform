"""Persistent local dataset identities; no uploads or production mutations."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .paths import validate_path


def dataset_fingerprint(path: str | Path) -> str:
    """Invalidate reports after file replacement, truncation, or normal edits.

    A metadata fingerprint is not a cryptographic content-integrity guarantee.
    Include directory entries as well so removal/addition invalidates the cache.
    """
    root = Path(path)
    digest = hashlib.sha256()
    for file in [root, *sorted(root.rglob("*"))]:
        stat = file.stat()
        digest.update(str(file.relative_to(root)).encode())
        digest.update(f"\0{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_ino}\n".encode())
    return digest.hexdigest()


def read_dataset_info(path: str | Path, roots) -> tuple[Path, dict]:
    """Cheap fresh metadata check for registered dataset/media requests.

    Registration and Doctor additionally inspect the full layout. A video Range
    request must not reopen every episode's Parquet metadata on every seek.
    """
    root = validate_path(path, roots)
    if not root.is_dir():
        raise ValueError("数据集路径必须是目录")
    if (root / ".dwp-incomplete").exists():
        raise ValueError("数据集尚未完成写入/验证（.dwp-incomplete），不能注册或用于后续操作")
    info_path = validate_path(root / "meta/info.json", [root])
    try:
        info = json.loads(info_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("无法读取数据集 meta/info.json") from error
    if not isinstance(info, dict) or info.get("codebase_version") not in {"v2.1", "v3.0"}:
        raise ValueError("仅支持 LeRobot v2.1 / v3.0 数据集")
    for key in ("total_episodes", "total_frames"):
        if type(info.get(key)) is not int or info[key] <= 0:
            raise ValueError(f"数据集尚不完整：{key} 必须为正整数")
    return root, info


def inspect_dataset(path: str | Path, roots) -> tuple[Path, dict]:
    """Check a complete supported layout, not its training/data quality.

    Broken numeric values remain inspectable by Doctor. Missing episode/video
    files, unsupported formats and references outside this dataset are rejected.
    """
    root, info = read_dataset_info(path, roots)
    # A dataset may contain internal symlinks, but never external references.
    for entry in root.rglob("*"):
        if entry.is_symlink():
            validate_path(entry, [root])
    try:
        import pyarrow.parquet as pq
        from letools.plugins import open_dataset
        source = open_dataset(root)
        if len(source.episodes) != info["total_episodes"]:
            raise ValueError("episode 元数据数量与 info.json 不一致")
        if sum(episode.length for episode in source.episodes) != info["total_frames"]:
            raise ValueError("episode 帧数与 info.json 不一致")
        shard_rows = {}
        for episode in source.episodes:
            data_path = validate_path(episode.data_path, [root])
            if not data_path.is_file():
                raise ValueError("缺少 episode Parquet 文件")
            if data_path not in shard_rows:
                shard_rows[data_path] = pq.read_metadata(data_path).num_rows
            start, end = episode.data_start, episode.data_end
            if (episode.length <= 0 or start < 0 or end - start != episode.length
                    or end > shard_rows[data_path]):
                raise ValueError("episode 数据切片超出 Parquet 范围")
            if info["codebase_version"] == "v2.1" and shard_rows[data_path] != episode.length:
                raise ValueError("episode Parquet 行数与元数据不一致")
            for key in source.metadata.video_keys:
                video = episode.videos.get(key)
                if video is None or not validate_path(video.path, [root]).is_file():
                    raise ValueError(f"缺少 episode 视频: {key}")
        if sum(shard_rows.values()) != info["total_frames"]:
            raise ValueError("Parquet 总行数与 info.json 不一致")
    except (OSError, KeyError, TypeError, ValueError, IndexError, AttributeError, ArithmeticError) as error:
        raise ValueError(f"数据集不完整或布局无效: {error}") from error
    return root, info


class DatasetRegistry:
    """register(path), get(id or local/id), all(); identities survive restarts."""

    def __init__(self, config):
        self.config = config
        self.path = Path(config.state_dir) / "datasets.json"

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text())
        if not isinstance(payload, list):
            raise ValueError("数据集注册表格式无效")
        return payload

    def register(self, path: str | Path) -> dict:
        """Validate a physical dataset and assign a stable local URL identity.

        Registration stores a reference and metadata; it neither copies the
        dataset nor uploads it. Identity derives from the canonical server path.
        """
        root, info = inspect_dataset(path, self.config.allowed_roots)
        slug = re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-")[:60] or "dataset"
        identity = f"{slug}-{hashlib.sha256(str(root).encode()).hexdigest()[:12]}"
        record = {"id": identity, "repo_id": f"local/{identity}", "path": str(root),
                  "name": Path(path).expanduser().name, "info": info}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.path.parent / "datasets.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self._read()
            # A deliberate physical-directory migration can preserve the old
            # public ID. Re-registering its new path must reuse that identity.
            existing = next((item for item in records if item["path"] == str(root)), None)
            if existing is not None:
                identity = existing["id"]
                record.update(id=identity, repo_id=existing["repo_id"])
            records = [item for item in records if item["id"] != identity]
            records.append(record)
            descriptor, temporary = tempfile.mkstemp(prefix="datasets-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(records, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return record

    def get(self, identity: str, *, deep: bool = False) -> dict:
        for record in self._read():
            if identity in (record["id"], record["repo_id"]):
                inspect = inspect_dataset if deep else read_dataset_info
                root, info = inspect(record["path"], self.config.allowed_roots)
                return {**record, "path": str(root), "info": info}
        raise KeyError(f"尚未注册的数据集: {identity}")

    def unregister(self, identities: list[str]) -> list[str]:
        """Remove registry references only; never open/delete dataset files.

        Uses the same interprocess lock as register. Repeating a removal is
        harmless and stale/unavailable datasets can also be unregistered.
        Re-registering their original path restores access with the same ID.
        """
        selected = set(identities)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.path.parent / "datasets.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self._read()
            removed = [item["id"] for item in records
                       if item["id"] in selected or item["repo_id"] in selected]
            remaining = [item for item in records if item["id"] not in removed]
            descriptor, temporary = tempfile.mkstemp(prefix="datasets-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(remaining, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return removed

    def all(self) -> list[dict]:
        result = []
        for record in self._read():
            try:
                root, info = read_dataset_info(record["path"], self.config.allowed_roots)
                result.append({**record, "path": str(root), "info": info})
            except (OSError, ValueError):
                # Keep the persisted identity for later recovery; do not expose a
                # dataset which has disappeared or is now incomplete as usable.
                continue
        return result

"""Canonical path checks shared by HTTP handlers and job preparation.

These checks constrain cooperative local filesystem access; the web process is
not a sandbox against another process with the same Unix account changing links
concurrently. Destructive jobs additionally reserve a new destination themselves.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

PathScope = Iterable[str | Path] | None


def validate_path(path: str | Path, roots: PathScope = None, must_exist: bool = True) -> Path:
    """Canonicalize a path, optionally confining internal dataset references.

    Without an explicit scope the OS enforces service-account permissions.
    Dataset media and MCAP manifests still supply their own containment scope.
    """
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError("路径不能为空")
    if any(ord(char) < 32 for char in str(path)):
        raise ValueError("路径中不能含控制字符")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("必须使用绝对路径")
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (FileNotFoundError, RuntimeError) as error:
        raise ValueError(f"无法解析路径: {candidate}") from error
    if roots is not None:
        allowed = [Path(root).expanduser().resolve() for root in roots]
        if not allowed or not any(resolved.is_relative_to(root) for root in allowed):
            raise ValueError("路径超出指定的数据范围（包括符号链接目标）")
    return resolved


def validate_output_path(
    path: str | Path, roots: PathScope = None, inputs: Iterable[str | Path] = (),
) -> Path:
    """Require a new output, disjoint from every input in both directions."""
    roots = tuple(roots) if roots is not None else None
    output = validate_path(path, roots, must_exist=False)
    if os.path.lexists(Path(path).expanduser()) or os.path.lexists(output):
        raise ValueError("输出路径已经存在；禁止覆盖，请选择全新的目录")
    for source in inputs:
        source = validate_path(source, roots)
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("输出不能等于输入、位于输入内部或成为输入的祖先目录")
    return output


def browse_directory(path: str | Path, roots: PathScope = None) -> dict:
    """List direct visible directories only, without traversing any recording."""
    roots = tuple(roots) if roots is not None else None
    current = validate_path(path, roots)
    if not current.is_dir():
        raise ValueError("只能浏览目录，不能浏览文件")
    directories = []
    for child in sorted(current.iterdir(), key=lambda item: item.name.casefold()):
        if child.name.startswith("."):
            continue
        try:
            target = validate_path(child, roots)
            if not target.is_dir():
                continue
            data = validate_path(target / "data", roots, must_exist=False)
            info = validate_path(target / "meta/info.json", roots, must_exist=False)
            is_dataset = info.is_file()
            directories.append({"name": child.name, "path": str(target),
                                "is_recording": data.is_dir() and not is_dataset,
                                "is_dataset": is_dataset})
        except (OSError, ValueError):
            continue
    try:
        parent = str(validate_path(current.parent, roots)) if current.parent != current else None
    except (OSError, ValueError):
        parent = None
    return {"path": str(current), "parent": parent, "dirs": directories,
            "directories": directories}


# Clear names for callers that use listing/output terminology.
list_directory = browse_directory
validate_output = validate_output_path


def suggest_directories(path: str, roots: PathScope = None) -> dict:
    """Complete one filesystem path segment using service-account permissions.

    Existing paths list children; a partial final component filters siblings.
    Return whether the typed path exists so the UI never selects its parent by
    mistake. No recursive walk or filesystem mutation occurs while typing.
    """
    roots = tuple(roots) if roots is not None else None
    raw = str(path).strip()
    # While an allowed root itself is only partly typed, offer that root alone
    # rather than listing its otherwise forbidden parent (e.g. /home/us...).
    root_matches = [Path(root).resolve() for root in (roots or ()) if str(Path(root).resolve()).startswith(raw)]
    if raw.startswith("/") and raw != "/" and root_matches and not any(str(p) == raw.rstrip("/") for p in root_matches):
        dirs = [{"name": p.name, "path": str(p), "is_dataset": False, "is_recording": False}
                for p in root_matches if p.is_dir()]
        return {"path": "", "parent": None, "dirs": dirs, "directories": dirs,
                "query": raw, "exact": False, "prefix": raw}
    candidate = validate_path(raw, roots, must_exist=False)
    if candidate.is_dir():
        return {**browse_directory(candidate, roots), "query": raw, "exact": True, "prefix": ""}
    parent = validate_path(Path(raw).expanduser().parent, roots)
    result = browse_directory(parent, roots)
    prefix = Path(raw).name
    dirs = [item for item in result["dirs"] if item["name"].casefold().startswith(prefix.casefold())]
    return {**result, "dirs": dirs, "directories": dirs, "query": raw, "exact": False, "prefix": prefix}


def create_directory(parent: str, name: str, roots: PathScope = None) -> Path:
    """Create one visible child directory, exclusively, outside recorded data.

    This creates an output *parent*. Final dataset names remain uncreated until
    validated publication. No existing directory is replaced or emptied.
    """
    if (not name.strip() or name != name.strip() or name.startswith(".")
            or any(char in name for char in "/\\") or any(ord(char) < 32 for char in name)):
        raise ValueError("目录名不可为空、以点开头或包含路径分隔符/控制字符")
    current = validate_path(parent, roots)
    if not current.is_dir():
        raise ValueError("父路径必须是已有目录")
    for ancestor in (current, *current.parents):
        if any((ancestor / marker).exists() for marker in (
                "meta/info.json", "data/metadata.yaml", "tianji-cache.json")):
            raise ValueError("不能在原始录制、准备缓存或已有数据集内部新建目录")
    target = validate_path(current / name, roots, must_exist=False)
    if os.path.lexists(current / name):
        raise FileExistsError("同名文件或目录已存在")
    target.mkdir(mode=0o755)
    return target

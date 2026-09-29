"""Canonical path checks shared by HTTP handlers and job preparation.

These checks constrain cooperative local filesystem access; the web process is
not a sandbox against another process with the same Unix account changing links
concurrently. Destructive jobs additionally reserve a new destination themselves.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

DEFAULT_ROOTS = (Path("/home/user"), Path("/media/user/BAG_STORAGE"))


def validate_path(path: str | Path, roots: Iterable[str | Path], must_exist: bool = True) -> Path:
    """Return an absolute canonical path contained in a canonical allowed root."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError("路径不能为空")
    if any(ord(char) < 32 for char in str(path)):
        raise ValueError("路径中不能含控制字符")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("必须使用绝对路径")
    try:
        resolved = candidate.resolve(strict=must_exist)
        allowed = [Path(root).expanduser().resolve() for root in roots]
    except (OSError, RuntimeError) as error:
        raise ValueError(f"无法解析路径: {candidate}") from error
    if not allowed or not any(resolved.is_relative_to(root) for root in allowed):
        raise ValueError("路径不在允许访问的目录内（包括符号链接目标）")
    return resolved


def validate_output_path(
    path: str | Path, roots: Iterable[str | Path], inputs: Iterable[str | Path] = (),
) -> Path:
    """Require a new output, disjoint from every input in both directions."""
    roots = tuple(roots)
    output = validate_path(path, roots, must_exist=False)
    if os.path.lexists(Path(path).expanduser()) or os.path.lexists(output):
        raise ValueError("输出路径已经存在；禁止覆盖，请选择全新的目录")
    for source in inputs:
        source = validate_path(source, roots)
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("输出不能等于输入、位于输入内部或成为输入的祖先目录")
    return output


def browse_directory(path: str | Path, roots: Iterable[str | Path]) -> dict:
    """List direct visible directories only, without traversing any recording."""
    roots = tuple(roots)
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
    except ValueError:
        parent = None
    return {"path": str(current), "parent": parent, "dirs": directories,
            "directories": directories}


# Clear names for callers that use listing/output terminology.
list_directory = browse_directory
validate_output = validate_output_path

"""Publish validated outputs without ever replacing another writer's output.

LeTools is invoked only inside a private, exclusively created staging parent.
Linux renameat2(RENAME_NOREPLACE) makes the final existence check and directory
publication one atomic operation. Unsupported systems fail closed.
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from pathlib import Path


def new_staging(destination: str | Path) -> Path:
    destination = Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(f"输出已存在，禁止覆盖: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix=f".{destination.name}.dwp-", dir=destination.parent))
    # parent is private (0700); the dataset child does not yet exist.
    return parent / "dataset"


def publish_noreplace(staging: str | Path, destination: str | Path) -> None:
    staging, destination = Path(staging), Path(destination)
    if sys.platform != "linux":
        raise RuntimeError("安全发布需要支持 renameat2 的 Linux；没有改动目标目录")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise RuntimeError("当前系统不支持原子防覆盖发布；没有改动目标目录")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    source_fd = os.open(staging.parent, flags)
    try:
        target_fd = os.open(destination.parent, flags)
        try:
            if rename(source_fd, os.fsencode(staging.name), target_fd,
                      os.fsencode(destination.name), 1) != 0:
                number = ctypes.get_errno()
                raise OSError(number, os.strerror(number), str(destination))
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)
    # Remove only the now-empty parent created by new_staging, never user data.
    try:
        staging.parent.rmdir()
    except OSError:
        pass

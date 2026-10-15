"""Publish validated outputs without ever replacing another writer's output.

LeTools is invoked only inside a private, exclusively created staging parent.
Linux renameat2(RENAME_NOREPLACE) makes the final existence check and directory
publication one atomic operation. NFS uses an exclusively created directory
symlink to the complete private dataset when the rename flag is unsupported.
The backing directory then becomes permanent storage, not disposable staging.
"""
from __future__ import annotations

import ctypes
import errno
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
                if number not in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
                    raise OSError(number, os.strerror(number), str(destination))
                _publish_link(staging, destination, source_fd, target_fd)
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


def _publish_link(staging, destination, source_fd, target_fd):
    """Atomic no-replace fallback for same-parent managed staging only.

    Unlike exists()+rename(), symlink creation cannot overwrite even an empty
    directory created by another client. A relative link keeps the collection
    portable as a whole. Never remove the backing parent after this succeeds.
    """
    import stat

    if (staging.name != "dataset" or not staging.parent.name.startswith(f".{destination.name}.dwp-")
            or staging.parent.parent.resolve() != destination.parent.resolve()):
        raise RuntimeError("文件系统不支持防覆盖改名，且暂存目录不是目标同级的受管理目录；未发布")
    source = os.stat(staging.name, dir_fd=source_fd, follow_symlinks=False)
    if not stat.S_ISDIR(source.st_mode):
        raise ValueError("发布源必须是完整的真实目录，不能是文件或符号链接")
    target_parent = os.fstat(target_fd)
    if source.st_dev != target_parent.st_dev:
        raise OSError(errno.EXDEV, "发布源与目标不在同一文件系统")
    # All readers see either no name or the complete verified dataset. There
    # is no writable reservation/partial directory at the final target name.
    relative = f"{staging.parent.name}/{staging.name}"
    os.symlink(relative, destination.name, dir_fd=target_fd)
    print(f"NAS-compatible publication: {destination} -> {relative}. "
          "Keep the hidden backing directory; it contains the published dataset.", flush=True)

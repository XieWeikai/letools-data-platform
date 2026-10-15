"""Publish validated outputs without ever replacing another writer's output.

LeTools is invoked only inside a private, exclusively created staging parent.
Linux renameat2(RENAME_NOREPLACE) makes the final existence check and directory
publication one atomic operation. Unsupported rename flags use an exclusively
created real directory with an incomplete marker and meta/info.json last.
"""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
import stat
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
                _publish_directory(staging, destination, source_fd, target_fd)
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


def _publish_directory(staging, destination, source_fd, target_fd):
    """Reserve a real directory; never overwrite an existing output or file.

    NFS cannot promise atomic directory rename with NOREPLACE. Readers in this
    platform reject the marker and external LeRobot readers lack info.json
    until all files are in place. A failed attempt retains both staging and a
    marked incomplete destination for diagnosis. It is not atomic rename.
    """
    if (staging.name != "dataset" or not staging.parent.name.startswith(f".{destination.name}.dwp-")
            or staging.parent.parent.resolve() != destination.parent.resolve()):
        raise RuntimeError("文件系统不支持防覆盖改名，且暂存目录不是目标同级的受管理目录；未发布")
    source = os.stat(staging.name, dir_fd=source_fd, follow_symlinks=False)
    if not stat.S_ISDIR(source.st_mode):
        raise ValueError("发布源必须是完整的真实目录，不能是文件或符号链接")
    target_parent = os.fstat(target_fd)
    if source.st_dev != target_parent.st_dev:
        raise OSError(errno.EXDEV, "发布源与目标不在同一文件系统")
    # Validate the complete source tree before reserving the destination. No
    # source links/special files are copied into the resulting real directory.
    directories, files = [], []
    for path in sorted(staging.rglob("*")):
        mode = path.lstat().st_mode
        relative = path.relative_to(staging)
        if relative == Path(".dwp-incomplete"):
            raise ValueError("拒绝发布未完成的源目录")
        if stat.S_ISDIR(mode):
            directories.append(relative)
        elif stat.S_ISREG(mode):
            files.append(relative)
        else:
            raise ValueError(f"发布源包含符号链接或特殊文件: {relative}")
    os.mkdir(destination.name, mode=0o700, dir_fd=target_fd)
    marker = destination / ".dwp-incomplete"
    with marker.open("x") as handle:
        handle.write(f"Publishing validated staging: {staging}\n")
        handle.flush()
        os.fsync(handle.fileno())
    for relative in directories:
        (destination / relative).mkdir(mode=0o700)
    # Hard links are ordinary files, not symlinks. After staging is removed
    # they remain complete files at the requested path, with no hidden backing.
    for relative in sorted(files, key=lambda p: p == Path("meta/info.json")):
        _link_or_copy(staging / relative, destination / relative)
    # Flush child directory entries before making the output visible to the
    # platform. No successful manifest is exposed while files are incomplete.
    for directory in [*(destination / p for p in reversed(directories)), destination]:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    marker.unlink()
    print(f"Published real directory without symlinks: {destination}", flush=True)
    # Publication has succeeded. Cleanup failure must not mislabel valid data
    # as a failed job; retain redundant staging and report its exact path.
    try:
        shutil.rmtree(staging)
    except OSError as error:
        print(f"Published successfully; redundant staging retained at {staging}: {error}", flush=True)


def _link_or_copy(source, destination):
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as error:
        if error.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.EPERM, errno.ENOSYS):
            raise
        # Exclusive creation also protects against a competing writer inside
        # the reserved directory. Disk/I/O/permission failures remain fatal.
        with source.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(source, destination, follow_symlinks=False)

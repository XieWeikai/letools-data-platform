import sys
import ctypes
import errno
from types import SimpleNamespace

import pytest

from data_web_platform.publishing import new_staging, publish_noreplace


def test_staging_refuses_existing_output(tmp_path):
    destination = tmp_path / "output"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        new_staging(destination)


@pytest.mark.skipif(sys.platform != "linux", reason="server uses Linux renameat2")
@pytest.mark.parametrize("collision", [None, "empty-dir", "populated-dir", "file", "symlink"])
def test_atomic_publish_never_replaces_concurrent_output(tmp_path, collision):
    destination = tmp_path / "output"
    staging = new_staging(destination)
    staging.mkdir()
    (staging / "verified.txt").write_text("new dataset")
    parent = staging.parent
    if collision in ("empty-dir", "populated-dir"):
        destination.mkdir()
        if collision == "populated-dir":
            (destination / "other.txt").write_text("someone else's output")
    elif collision == "file":
        destination.write_text("someone else's file")
    elif collision == "symlink":
        destination.symlink_to(tmp_path / "absent-target")
    if collision:
        with pytest.raises(FileExistsError):
            publish_noreplace(staging, destination)
        assert (staging / "verified.txt").read_text() == "new dataset"
        if collision == "populated-dir":
            assert (destination / "other.txt").read_text() == "someone else's output"
        if collision == "file":
            assert destination.read_text() == "someone else's file"
        if collision == "symlink":
            assert destination.is_symlink()
    else:
        publish_noreplace(staging, destination)
        assert (destination / "verified.txt").read_text() == "new dataset"
        assert not parent.exists()


@pytest.mark.parametrize("number", [errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS])
@pytest.mark.parametrize("collision", [None, "dir", "file", "dangling-link"])
def test_unsupported_rename_publishes_complete_link_without_overwrite(tmp_path, monkeypatch, number, collision):
    import data_web_platform.publishing as publishing
    class Rename:
        def __call__(self,*args):
            ctypes.set_errno(number)
            return -1
    monkeypatch.setattr(publishing.ctypes,"CDLL",lambda *a,**k:SimpleNamespace(renameat2=Rename()))
    destination=tmp_path/"output";staging=new_staging(destination);staging.mkdir()
    (staging/"data.txt").write_text("verified")
    if collision=="dir":destination.mkdir()
    elif collision=="file":destination.write_text("other writer")
    elif collision=="dangling-link":destination.symlink_to("missing")
    if collision:
        with pytest.raises(FileExistsError):publish_noreplace(staging,destination)
        if collision=="file":assert destination.read_text()=="other writer"
        if collision=="dangling-link":assert destination.readlink().as_posix()=="missing"
    else:
        publish_noreplace(staging,destination)
        assert destination.is_symlink() and destination.resolve()==staging
        assert (destination/"data.txt").read_text()=="verified"
        assert not destination.readlink().is_absolute()
    assert (staging/"data.txt").read_text()=="verified"


def test_permission_failure_does_not_switch_to_link(tmp_path,monkeypatch):
    import data_web_platform.publishing as publishing
    class Rename:
        def __call__(self,*args):ctypes.set_errno(errno.EACCES);return -1
    monkeypatch.setattr(publishing.ctypes,"CDLL",lambda *a,**k:SimpleNamespace(renameat2=Rename()))
    destination=tmp_path/"output";staging=new_staging(destination);staging.mkdir()
    with pytest.raises(PermissionError):publish_noreplace(staging,destination)
    assert staging.exists() and not destination.exists()


def test_fallback_refuses_unmanaged_source(tmp_path,monkeypatch):
    import data_web_platform.publishing as publishing
    class Rename:
        def __call__(self,*args):ctypes.set_errno(errno.EINVAL);return -1
    monkeypatch.setattr(publishing.ctypes,"CDLL",lambda *a,**k:SimpleNamespace(renameat2=Rename()))
    source=tmp_path/"unmanaged";source.mkdir()
    with pytest.raises(RuntimeError,match="受管理目录"):publish_noreplace(source,tmp_path/"output")
    assert source.is_dir()

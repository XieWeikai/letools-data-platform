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
def test_unsupported_rename_publishes_real_directory_without_overwrite(tmp_path, monkeypatch, number, collision):
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
        assert destination.is_dir() and not destination.is_symlink()
        assert (destination/"data.txt").read_text()=="verified"
        assert not staging.parent.exists()
        assert not (destination/'.dwp-incomplete').exists()
    if collision:assert (staging/"data.txt").read_text()=="verified"


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


def test_real_directory_failure_keeps_source_and_marker_and_no_info(tmp_path,monkeypatch):
    import data_web_platform.publishing as p
    class Rename:
        def __call__(self,*args):ctypes.set_errno(errno.EINVAL);return -1
    monkeypatch.setattr(p.ctypes,'CDLL',lambda *a,**k:SimpleNamespace(renameat2=Rename()))
    destination=tmp_path/'output';source=new_staging(destination);(source/'meta').mkdir(parents=True)
    (source/'meta/info.json').write_text('{"proof":true}')
    (source/'data.bin').write_bytes(b'original')
    def fail(*args):raise OSError(errno.ENOSPC,'disk full')
    monkeypatch.setattr(p,'_link_or_copy',fail)
    with pytest.raises(OSError,match='disk full'):publish_noreplace(source,destination)
    assert (source/'data.bin').read_bytes()==b'original'
    assert (destination/'.dwp-incomplete').exists()
    assert not (destination/'meta/info.json').exists()


def test_real_directory_info_is_last_and_files_share_then_keep_original_inode(tmp_path,monkeypatch):
    import data_web_platform.publishing as p
    class Rename:
        def __call__(self,*args):ctypes.set_errno(errno.EINVAL);return -1
    monkeypatch.setattr(p.ctypes,'CDLL',lambda *a,**k:SimpleNamespace(renameat2=Rename()))
    destination=tmp_path/'output';source=new_staging(destination);(source/'meta').mkdir(parents=True)
    (source/'meta/info.json').write_text('{}');(source/'data.bin').write_bytes(b'verified')
    inode=(source/'data.bin').stat().st_ino;calls=[];original=p._link_or_copy
    def inspect(src,dst):
        assert (destination/'.dwp-incomplete').exists()
        if src.name=='info.json':assert (destination/'data.bin').read_bytes()==b'verified'
        calls.append(src.name);original(src,dst)
    monkeypatch.setattr(p,'_link_or_copy',inspect)
    publish_noreplace(source,destination)
    assert calls[-1]=='info.json' and (destination/'data.bin').stat().st_ino==inode
    assert not source.parent.exists() and not any(v.is_symlink() for v in destination.rglob('*'))


def test_link_fallback_copies_exclusively(tmp_path,monkeypatch):
    import data_web_platform.publishing as p
    source=tmp_path/'source';source.write_bytes(b'original bytes')
    def unsupported(*a,**kw):raise OSError(errno.EOPNOTSUPP,'links unsupported')
    monkeypatch.setattr(p.os,'link',unsupported)
    dest=tmp_path/'destination';p._link_or_copy(source,dest)
    assert dest.read_bytes()==source.read_bytes() and not dest.is_symlink()
    with pytest.raises(FileExistsError):p._link_or_copy(source,dest)

import sys

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

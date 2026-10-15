"""Private subprocess: only derived preview files are written, never MCAPs."""
import json
import os
from pathlib import Path
import sys
import traceback

from .raw_data import build_cache, source_snapshot


def run(request_file):
    os.nice(10)
    request_file = Path(request_file)
    directory = request_file.parent
    request = json.loads(request_file.read_text())
    snapshot = request["snapshot"]
    def status(**fields):
        temporary = directory / "status.tmp"
        temporary.write_text(json.dumps(fields, ensure_ascii=False))
        temporary.replace(directory / "status.json")
    try:
        if source_snapshot(snapshot["path"], request["roots"]) != snapshot:
            raise ValueError("原始录制在开始预览前发生变化，请重新打开")
        result = build_cache(snapshot, directory, lambda **kw: status(state="running", **kw))
        if source_snapshot(snapshot["path"], request["roots"]) != snapshot:
            raise ValueError("原始录制在读取期间发生变化，拒绝发布旧索引")
        temporary = directory / "manifest.tmp"
        temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False))
        temporary.replace(directory / "manifest.json")
        status(state="ready", message="原始预览已就绪")
        return 0
    except Exception as error:
        status(state="failed", message=str(error))
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))

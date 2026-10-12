"""Account for every selected recording without guessing missing durations.

Raw duration is the numeric rosbag metadata duration, summed per recording (not
the wall-clock span between different recordings). Output duration is N / FPS,
including the last frame's display interval. These are deliberately different
measures: common coverage and stationary trimming can shorten accepted inputs.
"""
from pathlib import Path


def recording_durations(paths):
    import yaml

    entries = []
    for value in paths:
        path = Path(value)
        seconds = None
        try:
            metadata = yaml.safe_load((path / "data/metadata.yaml").read_text())
            nanos = metadata["rosbag2_bagfile_information"]["duration"]["nanoseconds"]
            if type(nanos) is int and nanos >= 0:
                seconds = nanos / 1e9
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
            # A malformed/missing file must still reach provider diagnostics.
            # An unknown duration is not a zero-second recording.
            pass
        entries.append({"path": str(path), "bag": path.name, "duration_seconds": seconds})
    return entries


def duration_group(entries):
    known = [item["duration_seconds"] for item in entries if item["duration_seconds"] is not None]
    return {"episodes": len(entries), "duration_seconds": sum(known) if len(known) == len(entries) else None,
            "known_duration_seconds": sum(known), "unknown_duration_episodes": len(entries) - len(known)}


def summarize(entries, report=None):
    summary = {"policy": "valid_only", "input": duration_group(entries),
               "raw_duration_basis": "data/metadata.yaml: rosbag2_bagfile_information.duration.nanoseconds",
               "output_duration_basis": "total_frames / fps", "output": None}
    if report is not None:
        results = report["results"]
        valid_indices = [index for index, result in enumerate(results) if result["valid"]]
        rejected = [{**entries[index], "source_recording_index": index, "issues": result["issues"]}
                    for index, result in enumerate(results) if not result["valid"]]
        summary.update(accepted=duration_group([entries[i] for i in valid_indices]),
                       skipped={**duration_group(rejected), "recordings": rejected})
    return summary


def verify_manifest_selection(manifest, report):
    """Fail closed if a prepared cache omits or includes the wrong recording.

    The provider sorts the indexed selection links before diagnostics. Its
    source indices therefore refer to the same order as the original request.
    Matching counts alone is insufficient when a bad input is in the middle.
    """
    expected = [i for i, item in enumerate(report["results"]) if item["valid"]]
    selection = manifest["source_selection"]
    actual = [(entry["source_recording_index"], entry["episode_index"])
              for entry in selection["episode_mapping"]]
    if selection["mode"] != "explicit_valid_only" or actual != list(zip(expected, range(len(expected)))):
        raise ValueError("准备缓存的有效轨迹映射与检查结果不一致，拒绝转换")
    episodes = manifest["episodes"]
    if len(episodes) != len(expected) or [e["index"] for e in episodes] != list(range(len(expected))):
        raise ValueError("准备缓存的 episode 数量或编号不一致")
    if [e["length"] for e in episodes] != [report["results"][i]["frames"] for i in expected]:
        raise ValueError("准备缓存的轨迹帧数与检查结果不一致")

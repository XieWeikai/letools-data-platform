"""Read-only Tianji action-name compatibility for upstream Doctor consumers.

The source is never renamed or patched. Action-dependent checks receive a loaded
dataset view; score/gate receive private, disposable metadata/action projections.
Only an explicit ``observation.action`` feature with no canonical action feature
is eligible. Existing action fields always retain their upstream semantics.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

SOURCE_ACTION = "observation.action"
ACTION = "action"
ACTION_CHECKS = frozenset({"actions", "per_episode", "training", "kinematics"})
ALIAS_MESSAGE = (
    "Web read-only compatibility: observation.action is inspected as action; "
    "source feature names, values and files are unchanged."
)


@contextmanager
def action_view(root: Path, *, workspace=None, tables=False):
    """Yield a narrowly scoped upstream input plus an auditable alias record.

    This is not a complete dataset and must never be registered, fixed or trimmed.
    No video copies, symlinks, global monkeypatches or user-provided code are used.
    Invalid stats remain invalid, and conflicting names fail closed.
    """
    info = json.loads((root / "meta/info.json").read_text())
    features = info.get("features", {})
    if SOURCE_ACTION not in features or any(key.startswith(ACTION) for key in features):
        yield root, None
        return
    if workspace is not None:
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="doctor-action-view-", dir=workspace) as temporary:
        view = Path(temporary)
        meta = view / "meta"
        meta.mkdir()
        projected_info = {**info, "features": {**features, ACTION: features[SOURCE_ACTION]}}
        (meta / "info.json").write_text(json.dumps(projected_info))
        stats_file = root / "meta/stats.json"
        if stats_file.exists():
            content = stats_file.read_text()
            try:
                stats = json.loads(content)
            except json.JSONDecodeError:
                # Do not manufacture usable stats over an upstream quality error.
                (meta / "stats.json").write_text(content)
            else:
                if ACTION in stats:
                    raise ValueError("Doctor action alias conflicts with existing action statistics")
                if SOURCE_ACTION in stats:
                    stats = {**stats, ACTION: stats[SOURCE_ACTION]}
                (meta / "stats.json").write_text(json.dumps(stats))
        note = {"source": SOURCE_ACTION, "target": ACTION, "read_only": True,
                "message": ALIAS_MESSAGE, "scope": sorted(ACTION_CHECKS) + ["score", "gate"]}
        if tables:
            # Gate only reads episode metadata; no video or unrelated payload is
            # copied. The source registry/deep inspection remains authoritative.
            episodes = root / "meta/episodes"
            if episodes.is_dir():
                for source in episodes.rglob("*.parquet"):
                    destination = meta / "episodes" / source.relative_to(episodes)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            _project_actions(root, view, features[SOURCE_ACTION])
            note["table_representation"] = "action.<dimension> scalar columns; original row order and values"
        yield view, note


def _project_actions(root, view, feature):
    """Preserve the T×D action matrix, including v3 episode shard boundaries.

    Upstream score flattens a single vector column; scalar dimension columns give
    it the same matrix without changing its scoring algorithm. Reassembling each
    episode also avoids losing the tail of an episode split over parquet shards.
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    shape = feature.get("shape")
    if not isinstance(shape, list) or len(shape) != 1 or not isinstance(shape[0], int) or shape[0] < 1:
        raise ValueError("observation.action must declare a nonempty one-dimensional action vector")
    dimensions = shape[0]
    shards: dict[int, list] = {}
    for source in sorted((root / "data").rglob("*.parquet")):
        names = pq.read_schema(source).names
        if any(name.startswith(ACTION) for name in names):
            raise ValueError("Doctor action alias conflicts with undeclared canonical action columns")
        if SOURCE_ACTION not in names or "episode_index" not in names:
            raise ValueError(f"Missing observation.action/episode_index in {source.name}")
        table = pq.read_table(source, columns=[SOURCE_ACTION, "episode_index"])
        try:
            values = np.asarray(table.column(SOURCE_ACTION).to_pylist(), dtype=np.float64)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid numeric observation.action in {source.name}") from error
        if values.ndim == 1 and dimensions == 1:
            values = values.reshape(-1, 1)
        if values.shape != (len(table), dimensions):
            raise ValueError(f"observation.action shape mismatch in {source.name}: {values.shape}")
        indices = table.column("episode_index").to_pylist()
        if any(not isinstance(index, int) or index < 0 for index in indices):
            raise ValueError(f"Invalid episode_index in {source.name}")
        projected = pa.table({"episode_index": indices,
                              **{f"action.{i}": values[:, i] for i in range(dimensions)}})
        episode_rows: dict[int, list[int]] = {}
        for row, index in enumerate(indices):
            episode_rows.setdefault(index, []).append(row)
        for index, rows in episode_rows.items():
            shards.setdefault(index, []).append(projected.take(rows))
    if not shards:
        raise ValueError("No observation.action rows available for Doctor")
    data = view / "data"
    data.mkdir()
    for index, pieces in shards.items():
        pq.write_table(pa.concat_tables(pieces), data / f"episode_{index:09d}.parquet")


def action_dataset_view(dataset, root):
    """Alias only declared command data; preserve all other loaded columns.

    The calling checks only read the shared arrays. New dataclass/dict containers
    prevent renaming the original view. An undeclared canonical column must not
    be silently overwritten: it could contain a different control signal.
    """
    for episode in dataset.episodes_data:
        if any(key.startswith(ACTION) for key in episode.columns):
            raise ValueError("Doctor action alias conflicts with undeclared canonical action columns")
    info = dataset.info
    features = {**info.features, ACTION: info.features[SOURCE_ACTION]}
    return replace(dataset, root=root,
        info=replace(info, root=root, raw={**info.raw, "features": features}, features=features),
        episodes_data=[replace(episode, columns={**episode.columns,
            **({ACTION: episode.columns[SOURCE_ACTION]} if SOURCE_ACTION in episode.columns else {})})
            for episode in dataset.episodes_data])


def run_score(root, *, max_episodes=None, drop_threshold=30, workspace=None):
    from lerobot_doctor.score import score_episodes
    with action_view(root, workspace=workspace, tables=True) as (view, note):
        report = asdict(score_episodes(view, max_episodes=max_episodes, drop_threshold=drop_threshold))
        if note:
            if not report["scores"]:
                raise ValueError("Doctor produced no action scores; check observation.action values")
            # Upstream can silently propagate NaN into scores; never emit a
            # successful JSON response containing non-finite quality scores.
            try:
                json.dumps(report, allow_nan=False)
            except ValueError as error:
                raise ValueError("Non-finite observation.action quality score; run Doctor check") from error
            report["compatibility"] = note
        return report


def run_gate(root, *, policy="act", custom_chunk_size=None, workspace=None):
    from lerobot_doctor.gate import gate_check
    with action_view(root, workspace=workspace, tables=True) as (view, note):
        report = asdict(gate_check(view, policy=policy, custom_chunk_size=custom_chunk_size))
        if note:
            report["info"].insert(0, ALIAS_MESSAGE)
            report["compatibility"] = note
        return report

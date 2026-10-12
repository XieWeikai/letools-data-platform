import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data_web_platform.datasets import dataset_fingerprint
from data_web_platform.doctor import run_check
from data_web_platform.doctor_compat import action_view, run_gate, run_score
from test_paths import make_dataset


def tianji_dataset(tmp_path, version="v2.1"):
    root = make_dataset(tmp_path / version, version=version, length=120)
    for file in (root / "meta/info.json", root / "meta/stats.json"):
        value = json.loads(file.read_text())
        features = value["features"] if file.name == "info.json" else value
        features["observation.action"] = features.pop("action")
        file.write_text(json.dumps(value))
    for file in (root / "data").rglob("*.parquet"):
        table = pq.read_table(file)
        pq.write_table(table.rename_columns(["observation.action" if name == "action" else name
                                            for name in table.column_names]), file)
    return root


def replace_actions(root, values):
    file = next((root / "data").rglob("*.parquet"))
    table = pq.read_table(file)
    table = table.set_column(table.column_names.index("observation.action"),
                             "observation.action", pa.array(values))
    pq.write_table(table, file)


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_alias_training_score_gate_are_readonly_and_auditable(tmp_path, version):
    root = tianji_dataset(tmp_path, version)
    before = dataset_fingerprint(root)
    workspace = tmp_path / "private-state"
    report = run_check(root, checks=["training"], workspace=workspace)
    assert report["checks"][0]["severity"] != "FAIL"
    assert "No action features" not in json.dumps(report)
    assert "Normalization statistics available" in json.dumps(report)
    assert report["compatibility"]["source"] == "observation.action"
    assert report["compatibility"]["read_only"] is True
    assert "Web read-only compatibility" in json.dumps(report["checks"])
    score = run_score(root, workspace=workspace)
    assert len(score["scores"]) == 1
    assert score["scores"][0]["episode_index"] == 0
    assert score["compatibility"]["source"] == "observation.action"
    gate = run_gate(root, workspace=workspace)
    # This fixture really has no images: compatibility must not erase that blocker.
    assert gate["passed"] is False
    assert any("requires image features" in blocker for blocker in gate["blockers"])
    assert "read-only compatibility" in gate["info"][0]
    assert before == dataset_fingerprint(root)
    assert list(workspace.iterdir()) == []
    assert "action" not in json.loads((root / "meta/info.json").read_text())["features"]


def test_projection_preserves_vector_dimensions_and_multishard_episode(tmp_path):
    from lerobot_doctor.score import _load_episodes
    root = tianji_dataset(tmp_path, "v3.0")
    file = next((root / "data").rglob("*.parquet"))
    original = pq.read_table(file)
    pq.write_table(original.slice(0, 47), file)
    pq.write_table(original.slice(47), file.with_name("file-001.parquet"))
    with action_view(root, workspace=tmp_path / "state", tables=True) as (view, _):
        episodes = _load_episodes(sorted((view / "data").rglob("*.parquet")), None)
        assert episodes[0]["actions"].shape == (120, 2)
        np.testing.assert_array_equal(episodes[0]["actions"], original.column("observation.action").to_pylist())


def test_missing_actions_remain_a_training_failure(tmp_path):
    root = make_dataset(tmp_path / "missing")
    file = root / "meta/info.json"
    info = json.loads(file.read_text())
    info["features"].pop("action")
    file.write_text(json.dumps(info))
    report = run_check(root, checks=["training"])
    assert report["overall_severity"] == "FAIL"
    assert "No action features" in json.dumps(report)
    assert "compatibility" not in report


@pytest.mark.parametrize("stats", ["invalid JSON", {"observation.action": {"mean": [0, 0], "std": [0, 0]}}])
def test_alias_does_not_suppress_bad_normalization(tmp_path, stats):
    root = tianji_dataset(tmp_path)
    (root / "meta/stats.json").write_text(stats if isinstance(stats, str) else json.dumps(stats))
    report = run_check(root, checks=["training"])
    assert report["overall_severity"] == ("FAIL" if isinstance(stats, str) else "WARN")
    assert ("invalid" if isinstance(stats, str) else "zero std") in json.dumps(report)


def test_nan_action_remains_failure_and_gate_blocker(tmp_path):
    root = tianji_dataset(tmp_path)
    values = [[i / 100, i / 200] for i in range(120)]
    values[8][1] = float("nan")
    replace_actions(root, values)
    report = run_check(root, checks=["actions", "training"])
    assert report["overall_severity"] == "FAIL"
    gate = run_gate(root)
    assert gate["passed"] is False
    assert any("NaN/Inf found in action.1" in message for message in gate["blockers"])
    with pytest.raises(ValueError, match="Non-finite"):
        run_score(root)


def test_existing_canonical_action_is_never_overridden(tmp_path):
    root = make_dataset(tmp_path / "both")
    info_file = root / "meta/info.json"
    info = json.loads(info_file.read_text())
    info["features"]["observation.action"] = info["features"]["action"]
    info_file.write_text(json.dumps(info))
    with action_view(root, tables=True) as (view, note):
        assert view == root and note is None
    report = run_check(root, checks=["training"])
    assert "compatibility" not in report


def test_declared_alias_with_missing_data_is_not_ready(tmp_path):
    root = tianji_dataset(tmp_path)
    file = next((root / "data").rglob("*.parquet"))
    pq.write_table(pq.read_table(file).drop_columns("observation.action"), file)
    report = run_check(root, checks=["training"])
    assert report["overall_severity"] == "FAIL"
    assert "observation.action is missing from data" in json.dumps(report)


@pytest.mark.parametrize("bad", ["stats_conflict", "column_conflict", "missing_column", "shape"])
def test_ambiguous_or_invalid_alias_fails_closed(tmp_path, bad):
    root = tianji_dataset(tmp_path)
    if bad == "stats_conflict":
        file = root / "meta/stats.json"
        stats = json.loads(file.read_text())
        stats["action"] = stats["observation.action"]
        file.write_text(json.dumps(stats))
    elif bad == "shape":
        replace_actions(root, [[0., 0., 0.] for _ in range(120)])
    else:
        file = next((root / "data").rglob("*.parquet"))
        table = pq.read_table(file)
        table = (table.append_column("action", table.column("observation.action"))
                 if bad == "column_conflict" else table.drop_columns("observation.action"))
        pq.write_table(table, file)
    with pytest.raises(ValueError, match="conflicts|Missing|shape mismatch"):
        run_score(root)


def test_compatibility_does_not_change_other_checks(tmp_path):
    from lerobot_doctor.dataset_loader import load_local
    from lerobot_doctor.report import report_to_json
    from lerobot_doctor.runner import run_checks
    root = tianji_dataset(tmp_path)
    checks = ["statistics", "temporal", "training"]
    original = json.loads(report_to_json(run_checks(load_local(root), checks=checks)))
    adapted = run_check(root, checks=checks)
    assert adapted["checks"][:2] == original["checks"][:2]
    assert original["checks"][2]["severity"] == "FAIL"
    assert adapted["checks"][2]["severity"] != "FAIL"


def add_wrench_windows(root, *, nan=False):
    """Real provider shape: one [4, 12] high-rate history per dataset row."""
    key = "observation.wrench_windows"
    info_file = root / "meta/info.json"
    info = json.loads(info_file.read_text())
    info["features"][key] = {"dtype": "float32", "shape": [4, 12]}
    info_file.write_text(json.dumps(info))
    for file in (root / "data").rglob("*.parquet"):
        table = pq.read_table(file)
        values = np.arange(len(table) * 4 * 12, dtype=np.float32).reshape(-1, 4, 12) / 1000
        if nan:
            values[7, 2, 3] = np.nan
        pq.write_table(table.append_column(key, pa.array(values.tolist())), file)


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_all_checks_support_nested_wrench_without_changing_source(tmp_path, version):
    from data_web_platform.doctor import CHECKS
    root = tianji_dataset(tmp_path, version)
    add_wrench_windows(root)
    before = dataset_fingerprint(root)
    report = run_check(root)
    assert len(report["checks"]) == len(CHECKS) == 12
    assert report["dataset_path"] == str(root)
    assert report["dataset_name"] == root.name
    assert report["compatibility"]["applied_checks"] == ["actions", "training", "per_episode", "kinematics"]
    assert before == dataset_fingerprint(root)


def test_wrench_nan_is_still_diagnosed_by_original_statistics(tmp_path):
    root = tianji_dataset(tmp_path)
    add_wrench_windows(root, nan=True)
    report = run_check(root)
    distribution = next(item for item in report["checks"] if item["name"] == "Data Distribution")
    assert distribution["severity"] == "FAIL"
    assert "observation.wrench_windows: 1 NaN values" in json.dumps(distribution)


def test_action_checks_match_canonical_commands_not_other_observations(tmp_path):
    from lerobot_doctor.dataset_loader import load_local
    from lerobot_doctor.report import report_to_json
    from lerobot_doctor.runner import run_checks
    from data_web_platform.doctor_compat import ALIAS_MESSAGE
    canonical = make_dataset(tmp_path / "canonical", length=120)
    root = tianji_dataset(tmp_path)
    add_wrench_windows(root)
    selected = ["actions", "per_episode"]
    expected = json.loads(report_to_json(run_checks(load_local(canonical), checks=selected)))
    actual = run_check(root, checks=selected)
    for item in actual["checks"]:
        item["messages"] = [message for message in item["messages"] if message["message"] != ALIAS_MESSAGE]
        for message in item["messages"]:
            if message["severity"] != "PASS":
                assert message["explanation"]["meaning"]
                message.pop("explanation")  # Added UI help must not alter upstream evidence.
    assert actual["checks"] == expected["checks"]


def test_check_never_overwrites_an_undeclared_action_column(tmp_path):
    root = tianji_dataset(tmp_path)
    file = next((root / "data").rglob("*.parquet"))
    table = pq.read_table(file)
    pq.write_table(table.append_column("action", table.column("observation.action")), file)
    before = dataset_fingerprint(root)
    with pytest.raises(ValueError, match="undeclared canonical action"):
        run_check(root)
    assert before == dataset_fingerprint(root)


def test_missing_commands_with_nested_wrench_report_failure_not_exception(tmp_path):
    root = tianji_dataset(tmp_path)
    add_wrench_windows(root)
    file = next((root / "data").rglob("*.parquet"))
    pq.write_table(pq.read_table(file).drop_columns("observation.action"), file)
    report = run_check(root)
    assert len(report["checks"]) == 12
    assert report["overall_severity"] == "FAIL"
    assert "observation.action is missing from data" in json.dumps(report)

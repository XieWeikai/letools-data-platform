"""Persistence, path boundaries, complete config and evidence interpretation."""
from dataclasses import fields
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_web_platform.datasets import DatasetRegistry, dataset_fingerprint
from data_web_platform.explanations import annotate_job, explain_diagnostic, explain_issue
from data_web_platform.models import AlignmentParams, JobRequest
from data_web_platform.paths import create_directory, suggest_directories
from data_web_platform.ui_help import WORKBENCH, DOCTOR, CHECK_HELP, FIX_HELP
from data_web_platform.doctor import DoctorRequest, CHECKS, FIXES
from test_paths import make_dataset
from test_jobs import manager


def test_published_directory_link_keeps_user_facing_name(tmp_path):
    physical=make_dataset(tmp_path/".published.dwp-storage"/"dataset")
    public=tmp_path/"my_dataset";public.symlink_to(physical,target_is_directory=True)
    registry=DatasetRegistry(SimpleNamespace(state_dir=tmp_path/"state",allowed_roots=[tmp_path]))
    record=registry.register(public)
    assert record['name']=='my_dataset' and record['path']==str(physical.resolve())
    assert registry.get(record['id'])['name']=='my_dataset'


def test_reregister_migrated_directory_preserves_public_id(tmp_path):
    root=make_dataset(tmp_path/'real-dataset')
    registry=DatasetRegistry(SimpleNamespace(state_dir=tmp_path/'state',allowed_roots=[tmp_path]))
    record=registry.register(root)
    record.update(id='preserved-id',repo_id='local/preserved-id')
    registry.path.write_text(json.dumps([record]))
    assert registry.register(root)['id']=='preserved-id'
    assert len(registry.all())==1


def test_unregister_batch_is_reference_only_and_reversible(tmp_path):
    paths = [make_dataset(tmp_path / name) for name in ("one", "two", "three")]
    registry = DatasetRegistry(SimpleNamespace(state_dir=tmp_path / "state", allowed_roots=[tmp_path]))
    records = [registry.register(path) for path in paths]
    before = [dataset_fingerprint(path) for path in paths]
    assert registry.unregister([records[0]['id'], records[1]['repo_id']]) == [records[0]['id'], records[1]['id']]
    assert [item['id'] for item in registry.all()] == [records[2]['id']]
    assert registry.unregister([records[0]['id']]) == []
    assert before == [dataset_fingerprint(path) for path in paths]
    assert registry.register(paths[0])['id'] == records[0]['id']
    assert len(registry.all()) == 2


def test_unregister_unavailable_entry_does_not_read_the_dataset(tmp_path):
    registry = DatasetRegistry(SimpleNamespace(state_dir=tmp_path / "state", allowed_roots=[tmp_path]))
    path = make_dataset(tmp_path / "one")
    record = registry.register(path)
    (path / "meta/info.json").unlink()
    assert not registry.all()
    assert registry.unregister([record['id']]) == [record['id']]


def test_clear_history_keeps_files_reports_and_survives_restart(tmp_path):
    jobs = manager(tmp_path)
    ids = [jobs.submit({'kind':'check'})['id'] for _ in range(2)]
    for identity in ids:
        jobs.records[identity]['status'] = 'succeeded'
        jobs._save(jobs.records[identity])
    before = {str(p):p.read_bytes() for p in jobs.root.rglob('*') if p.is_file()}
    assert jobs.clear_history(ids) == sorted(ids)
    assert not jobs.all() and jobs.get(ids[0])['status'] == 'succeeded'
    assert not manager(tmp_path).all()
    assert before == {str(p):p.read_bytes() for p in jobs.root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('status', ['queued','running','cancelled_process'])
def test_active_mixed_clear_is_rejected_atomically(tmp_path, status):
    jobs = manager(tmp_path)
    one = jobs.submit({'kind':'check'})['id']; two = jobs.submit({'kind':'check'})['id']
    jobs.records[one]['status'] = 'succeeded'
    jobs.records[two]['status'] = 'cancelled' if status == 'cancelled_process' else status
    if status == 'cancelled_process': jobs.processes[two] = object()
    with pytest.raises(ValueError, match='不能清除'):
        jobs.clear_history([one,two])
    assert len(jobs.all()) == 2 and not jobs.hidden_path.exists()


def test_failed_history_write_does_not_hide_memory_state(tmp_path, monkeypatch):
    jobs = manager(tmp_path)
    identity = jobs.submit({'kind':'check'})['id']
    jobs.records[identity]['status'] = 'succeeded'
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr('data_web_platform.jobs.atomic_json',fail)
    with pytest.raises(OSError,match='disk full'): jobs.clear_history([identity])
    assert len(jobs.all()) == 1


def test_partial_directory_completion_and_root_boundary(tmp_path):
    allowed = tmp_path / 'allowed'; allowed.mkdir()
    for name in ('pick_and_place','plug_wire','unrelated'): (allowed/name).mkdir()
    result = suggest_directories(str(allowed/'pi'),[allowed])
    assert not result['exact'] and [d['name'] for d in result['dirs']] == ['pick_and_place']
    assert suggest_directories(str(allowed/'pick_and_place'),[allowed])['exact']
    result = suggest_directories(str(tmp_path/'all'),[allowed])
    assert [d['path'] for d in result['dirs']] == [str(allowed)] and result['path'] == ''
    with pytest.raises(ValueError): suggest_directories('/etc/p',[allowed])
    (allowed/'escape').symlink_to(tmp_path, target_is_directory=True)
    assert not suggest_directories(str(allowed/'esc'),[allowed])['dirs']
    with pytest.raises(ValueError): suggest_directories(str(allowed/'escape'),[allowed])


def test_mkdir_creates_one_directory_without_replacing_files(tmp_path):
    parent = tmp_path/'out'; parent.mkdir()
    assert create_directory(str(parent),'batch one',[tmp_path]) == parent/'batch one'
    with pytest.raises(FileExistsError): create_directory(str(parent),'batch one',[tmp_path])
    for name in ('../escape','a/b','.hidden','', ' leading'):
        with pytest.raises(ValueError): create_directory(str(parent),name,[tmp_path])
    dataset = make_dataset(tmp_path/'source')
    with pytest.raises(ValueError,match='内部'): create_directory(str(dataset/'data'),'bad',[tmp_path])
    (parent/'escape').symlink_to('/etc',target_is_directory=True)
    with pytest.raises(ValueError): create_directory(str(parent/'escape'),'new',[tmp_path])


@pytest.mark.parametrize('rates', [(50,200),(50,400),(100,100),(25,1000)])
def test_wrench_frequency_matches_provider_contract(rates):
    config = AlignmentParams(fps=rates[0],wrench_fps=rates[1])
    assert config.wrench_fps == rates[1]


@pytest.mark.parametrize('rates',[(50,30),(50,201),(0,200),(50,1001)])
def test_invalid_wrench_frequency_is_rejected(rates):
    with pytest.raises(ValueError): AlignmentParams(fps=rates[0],wrench_fps=rates[1])


def test_all_public_provider_parameters_are_exposed_and_explained():
    from tianji_provider.provider import TianjiConfig
    assert set(AlignmentParams.model_fields) == {field.name for field in fields(TianjiConfig)}
    assert all(name.replace('_','-') in WORKBENCH for name in AlignmentParams.model_fields)
    assert all(name in DOCTOR for name in DoctorRequest.model_fields if name not in {'checks','fixes'})
    assert set(CHECK_HELP) == set(CHECKS) and set(FIX_HELP) == set(FIXES)


def test_explanations_preserve_raw_severity_and_evidence():
    issue={'code':'observation_gap','severity':'error','message':'raw English','gap_ms':100.85,'threshold_ms':100}
    original=dict(issue)
    value=explain_issue(issue)
    assert issue==original and '100.85' in value['evidence'] and '跳过' in value['effect']
    assert '处理说明' in explain_issue({'code':'command_pause_filled','severity':'info'})['effect']
    assert '没有足够' in explain_diagnostic('info.json missing')['meaning']  # Not a false Inf match.
    unknown=explain_issue({'code':'new_check','severity':'warning'})
    assert '尚未' in unknown['meaning'] and '以轨迹valid' in unknown['effect']
    job={'check_results':[{'issues':[dict(issue)]}],'check_report':{'valid':False,'results':[]}}
    result=annotate_job(job)
    assert result['check_report']['valid'] is False
    assert result['check_results'][0]['issues'][0]['message']=='raw English'

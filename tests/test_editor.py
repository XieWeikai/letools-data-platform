import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from data_web_platform.datasets import DatasetRegistry, dataset_fingerprint
from data_web_platform.editor import (EditorRequest, VideoOptions, create_editor_router,
                                     edit_config, inspect_editor, prepare_editor_request)
from data_web_platform.editor_worker import run_prepared
from test_paths import make_dataset


@pytest.fixture
def context(tmp_path):
    config = SimpleNamespace(allowed_roots=[tmp_path], state_dir=tmp_path/'state')
    source = make_dataset(tmp_path/'source')
    registry = DatasetRegistry(config)
    record = registry.register(source)
    request = {'dataset': record['id'], 'output': str(tmp_path/'edited'), 'operation':'plan',
               'source_fingerprint':dataset_fingerprint(source), 'task_by_episode':{'0':'New task'}}
    records = {}
    jobs = SimpleNamespace(get=lambda key: records[key], submit_editor=lambda r: {'id':'a'*32,'request':r})
    return config, source, registry, record, request, jobs, records


@pytest.mark.parametrize('changes', [
    {'overwrite':True}, {'ffmpeg':'/tmp/evil'}, {'operation':'shell'}, {'workers':True},
    {'workers':9}, {'delete_episodes':[True]}, {'delete_episodes':[-1]},
    {'task_by_episode':{'01':'bad'}}, {'task_by_episode':{'0':' '}},
    {'delete_episodes':[0]}, {'video':{'codec':'shell'}},
    {'video':{'codec':'mjpeg','crf':18}}, {'video':{'codec':'libx264','quality':2}},
    {'video':{'size':[225,224]}}, {'operation':'apply'}, {'batch_rows':0},
])
def test_allowlisted_requests(context, changes):
    with pytest.raises(ValidationError):
        EditorRequest.model_validate({**context[4], **changes})


def test_noop_and_empty_video_keys(context):
    request = context[4]
    with pytest.raises(ValidationError):
        EditorRequest.model_validate({**request,'task_by_episode':{}})
    video = VideoOptions(codec='mjpeg',size=(224,224),quality=3,video_keys=['camera'])
    config = edit_config(EditorRequest.model_validate({**request,'video':video.model_dump()}))
    assert config.video.keys == ('camera',) and config.video.quality == 3
    assert config.ffmpeg == '/usr/bin/ffmpeg' and config.overwrite is False


def test_readonly_inspection_and_pagination(context):
    config,source,registry,record,_,_,_=context
    before=dataset_fingerprint(source)
    result=inspect_editor(record['id'],registry,config)
    assert result['episodes']==1 and result['episode_preview'][0]['length']==20
    assert result['source_fingerprint']==before
    assert inspect_editor(record['id'],registry,config,10,20)['episode_preview']==[]
    assert dataset_fingerprint(source)==before


def test_apply_requires_exact_successful_plan(context):
    config,source,registry,record,payload,jobs,records=context
    prepared=prepare_editor_request(payload,registry,config,jobs)
    assert prepared['destination'] is None
    records['a'*32]={'kind':'editor','status':'succeeded','request':prepared,'result':{'plan':{'episodes':1}}}
    apply={**payload,'operation':'apply','plan_job_id':'a'*32}
    assert prepare_editor_request(apply,registry,config,jobs)['destination']==payload['output']
    for delta in ({'output':str(source.parent/'other')},{'task_by_episode':{'0':'Other'}},{'workers':2}):
        with pytest.raises(ValueError,match='不一致'):
            prepare_editor_request({**apply,**delta},registry,config,jobs)
    records['a'*32]['status']='failed'
    with pytest.raises(ValueError):prepare_editor_request(apply,registry,config,jobs)
    (source/'changed.txt').write_text('changed')
    with pytest.raises(ValueError,match='变化'):prepare_editor_request(payload,registry,config,jobs)


def test_path_guards_and_symlinks(context):
    config,source,registry,_,payload,jobs,_=context
    for path in (source,source/'nested',source.parent,Path('/etc/new-output')):
        with pytest.raises(ValueError):prepare_editor_request({**payload,'output':str(path)},registry,config,jobs)
    (source/'link').symlink_to(source/'meta/info.json')
    with pytest.raises(ValueError,match='符号链接'):prepare_editor_request(payload,registry,config,jobs)


def test_router_missing_dataset_plan_apply_and_page(context):
    config,source,registry,_,payload,jobs,_=context
    app=FastAPI();app.include_router(create_editor_router(config,registry,jobs));client=TestClient(app)
    assert client.get('/editor').status_code==200
    assert client.get('/api/editor/options').json()['available']
    assert client.get('/api/editor/inspect',params={'dataset':payload['dataset']}).status_code==200
    assert client.get('/api/editor/inspect',params={'dataset':'missing'}).status_code==404
    assert client.get('/api/editor/inspect',params={'dataset':payload['dataset'],'limit':100000}).status_code==422
    assert client.post('/api/editor/jobs',json=payload).status_code==202
    assert client.post('/api/editor/jobs',json={**payload,'overwrite':True}).status_code==422


def publish_for_test(staging,destination):
    """Only replace Linux publish for macOS tests; caller has validated a new output."""
    if destination.exists():raise FileExistsError(destination)
    staging.rename(destination)


@pytest.mark.parametrize('version',['v2.1','v3.0'])
def test_real_task_feature_edit_and_unchanged_source(tmp_path,monkeypatch,capsys,version):
    from letools import open_dataset,validate_dataset
    source=make_dataset(tmp_path/'input',version)
    before=dataset_fingerprint(source)
    cfg=SimpleNamespace(allowed_roots=[tmp_path],state_dir=tmp_path/'state')
    registry=DatasetRegistry(cfg);record=registry.register(source)
    payload={'dataset':record['id'],'output':str(tmp_path/'edited'),'source_fingerprint':before,
             'task_by_episode':{'0':'Updated task'},'remove_features':['action']}
    records={};jobs=SimpleNamespace(get=lambda key:records[key])
    plan_request=prepare_editor_request(payload,registry,cfg,jobs)
    workspace=tmp_path/'job';workspace.mkdir()
    assert run_prepared(plan_request,workspace)==0
    assert not Path(payload['output']).exists()
    events=[json.loads(line[10:]) for line in capsys.readouterr().out.splitlines() if line.startswith('DWP_EVENT ')]
    result=next(e['result'] for e in events if e['event']=='result')
    records['a'*32]={'kind':'editor','status':'succeeded','request':plan_request,'result':result}
    apply=prepare_editor_request({**payload,'operation':'apply','plan_job_id':'a'*32},registry,cfg,jobs)
    monkeypatch.setattr('data_web_platform.editor_worker.publish_noreplace',publish_for_test)
    assert run_prepared(apply,workspace)==0
    output=open_dataset(payload['output']);assert output.metadata.version==version
    assert output.episodes[0].tasks==('Updated task',)
    assert 'action' not in output.metadata.features
    assert validate_dataset(payload['output'],deep=True).valid
    assert dataset_fingerprint(source)==before


@pytest.mark.parametrize('failure',['source_changed','validation','destination_race'])
def test_failed_edit_never_publishes(context,tmp_path,monkeypatch,failure):
    import letools_editor
    from letools.validation import validate_dataset as validate
    config,source,registry,_,payload,jobs,records=context
    prepared=prepare_editor_request(payload,registry,config,jobs)
    prepared['options'].update(operation='apply',plan_job_id='a'*32)
    original=letools_editor.edit_dataset
    def edit(src,dst,cfg):
        result=original(src,dst,cfg)
        if failure=='source_changed':(src/'external.txt').write_text('other writer')
        if failure=='destination_race':Path(payload['output']).mkdir()
        return result
    monkeypatch.setattr(letools_editor,'edit_dataset',edit)
    if failure=='validation':
        def bad(path,**kw):
            report=validate(path,**kw);report.valid=False;report.errors=['injected'];return report
        monkeypatch.setattr('letools.validation.validate_dataset',bad)
    monkeypatch.setattr('data_web_platform.editor_worker.publish_noreplace',publish_for_test)
    workspace=tmp_path/'job';workspace.mkdir()
    with pytest.raises((ValueError,FileExistsError)):run_prepared(prepared,workspace)
    assert not (Path(payload['output'])/'meta/info.json').exists()


def test_editor_uses_existing_queue_and_reserves_apply_output(tmp_path):
    from data_web_platform.config import Config
    from data_web_platform.jobs import JobManager
    cfg=Config(project_root=tmp_path,state_dir=tmp_path/'state',allowed_roots=[tmp_path])
    manager=JobManager(cfg)
    plan=manager.submit_editor({'kind':'editor','operation':'plan','destination':None})
    assert plan['module']=='data_web_platform.editor_worker'
    output=str(tmp_path/'edited')
    first=manager.submit_editor({'kind':'editor','operation':'apply','destination':output})
    with pytest.raises(ValueError,match='另一个'):
        manager.submit_editor({'kind':'editor','operation':'apply','destination':output})
    assert manager.cancel(first['id'])['status']=='cancelled'
    assert manager.submit_editor({'kind':'editor','operation':'apply','destination':output})['status']=='queued'

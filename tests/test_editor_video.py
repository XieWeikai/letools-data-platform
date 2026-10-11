"""Actual editor/FFmpeg acceptance through the platform adapter in both layouts."""
import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from letools import convert, ConversionConfig, open_dataset, validate_dataset
from letools._stats import aggregate_episode_stats
from data_web_platform.datasets import dataset_fingerprint, DatasetRegistry
from data_web_platform.editor import prepare_editor_request
from data_web_platform.editor_worker import run_prepared
from test_paths import make_dataset
from test_editor import publish_for_test

CAMERA='observation.image.head_left'


def make_video_source(root, version):
    source=make_dataset(root/'v21',length=6)
    info=json.loads((source/'meta/info.json').read_text())
    base=pq.read_table(source/'data/chunk-000/episode_000000.parquet')
    base_stats=json.loads((source/'meta/episodes_stats.jsonl').read_text())['stats']
    stats=[];episodes=[]
    info.update(total_episodes=3,total_frames=18,total_videos=3,splits={'train':'0:3'},
                video_path='videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4')
    info['features'][CAMERA]={'dtype':'video','shape':[24,32,3],'names':['height','width','channels'],
        'info':{'video.fps':50,'video.codec':'h264','video.pix_fmt':'yuv420p','video.is_depth_map':False,'has_audio':False}}
    for index in range(3):
        table=base.set_column(base.schema.get_field_index('episode_index'),'episode_index',pa.array([index]*6))
        table=table.set_column(table.schema.get_field_index('index'),'index',pa.array(np.arange(6)+6*index))
        pq.write_table(table,source/f'data/chunk-000/episode_{index:06d}.parquet')
        video=source/f'videos/chunk-000/{CAMERA}/episode_{index:06d}.mp4';video.parent.mkdir(parents=True,exist_ok=True)
        with av.open(str(video),'w') as out:
            stream=out.add_stream('libx264',rate=50);stream.width=32;stream.height=24;stream.pix_fmt='yuv420p';stream.options={'bf':'0','crf':'18'}
            for frame in range(6):
                rgb=np.full((24,32,3),index*60+frame*3,dtype=np.uint8)
                for packet in stream.encode(av.VideoFrame.from_ndarray(rgb,format='rgb24')):out.mux(packet)
            for packet in stream.encode():out.mux(packet)
        epstats={**base_stats,CAMERA:{k:np.full((3,1,1),v).tolist() for k,v in [('min',0),('max',1),('mean',.5),('std',.2)]}}
        epstats[CAMERA]['count']=[6]
        stats.append({'episode_index':index,'stats':epstats});episodes.append({'episode_index':index,'length':6,'tasks':['test']})
    (source/'meta/info.json').write_text(json.dumps(info));(source/'meta/episodes.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in episodes))
    (source/'meta/episodes_stats.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in stats))
    (source/'meta/stats.json').write_text(json.dumps(aggregate_episode_stats([e['stats'] for e in stats])))
    if version=='v2.1':return source
    target=root/'v30';convert(source,target,'v3.0',config=ConversionConfig(workers=1,video_workers=1));return target


def hashes(root):return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('version',['v2.1','v3.0'])
@pytest.mark.parametrize('mode',['reuse','libx264','mjpeg','remove_camera'])
def test_fused_real_video_editor(tmp_path,monkeypatch,capsys,version,mode):
    source=make_video_source(tmp_path/'source',version);before=hashes(source)
    cfg=SimpleNamespace(allowed_roots=[tmp_path],state_dir=tmp_path/'state');registry=DatasetRegistry(cfg);record=registry.register(source)
    payload={'dataset':record['id'],'output':str(tmp_path/'output'),'source_fingerprint':dataset_fingerprint(source),
             'delete_episodes':[1],'task_by_episode':{'2':'Place object'},'remove_features':['action'], 'workers':1,'codec_threads':1}
    if mode=='remove_camera':payload['remove_features'].append(CAMERA)
    elif mode!='reuse':payload['video']={'codec':mode,'size':[16,16],'video_keys':[CAMERA]}
    import data_web_platform.editor_worker as worker
    original=worker.edit_config
    monkeypatch.setattr(worker,'edit_config',lambda request:replace(original(request),ffmpeg=shutil.which('ffmpeg')))
    monkeypatch.setattr(worker,'publish_noreplace',publish_for_test)
    requests={};jobs=SimpleNamespace(get=lambda key:requests[key]);plan=prepare_editor_request(payload,registry,cfg,jobs)
    workspace=tmp_path/'job';workspace.mkdir();assert run_prepared(plan,workspace)==0
    events=[json.loads(line[10:]) for line in capsys.readouterr().out.splitlines() if line.startswith('DWP_EVENT ')]
    requests['a'*32]={'kind':'editor','status':'succeeded','request':plan,'result':next(e['result'] for e in events if e['event']=='result')}
    apply=prepare_editor_request({**payload,'operation':'apply','plan_job_id':'a'*32},registry,cfg,jobs)
    assert run_prepared(apply,workspace)==0
    output=open_dataset(payload['output']);assert len(output.episodes)==2 and output.metadata.total_frames==12
    assert output.episodes[1].tasks==('Place object',);assert 'action' not in output.metadata.features
    assert output.metadata.fps==50 and output.metadata.version==version
    assert validate_dataset(payload['output'],deep=True).valid
    for ep in output.episodes:
        table=output.read_episode(ep);assert table['frame_index'].to_pylist()==list(range(6))
        if mode=='remove_camera':assert not ep.videos;continue
        media=ep.videos[CAMERA]
        with av.open(str(media.path)) as container:
            decoded=[f for f in container.decode(video=0) if media.start-1e-6<=f.time<media.end-1e-6]
        assert len(decoded)==6
        assert [(f.width,f.height) for f in decoded]==([(16,16)]*6 if mode in ['libx264','mjpeg'] else [(32,24)]*6)
        if mode in ['libx264','mjpeg']:
            # Match the documented FFmpeg RGB color conversion used for stats;
            # PyAV bundles a different libswscale and can round limited-range RGB differently.
            raw=subprocess.check_output([shutil.which('ffmpeg'),'-v','error','-i',str(media.path),
                                         '-map','0:v:0','-fps_mode','passthrough','-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
            all_rgb=np.frombuffer(raw,dtype=np.uint8).reshape(-1,16,16,3)
            rgb=all_rgb[round(media.start*50):round(media.end*50)]/255
            assert len(rgb)==6
            np.testing.assert_allclose(np.asarray(ep.stats[CAMERA]['mean']).ravel(),rgb.mean(axis=(0,1,2)),atol=1e-8)
    assert hashes(source)==before

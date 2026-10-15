"""Raw inspection never inherits conversion filtering, normalization or filling."""
from fractions import Fraction
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import av
from fastapi.testclient import TestClient
import numpy as np
import pytest
import yaml

from data_web_platform.app import create_app
from data_web_platform.config import Config
from data_web_platform.raw_data import (CAMERAS, FORMAT, build_cache, clock_info, header_time,
    numeric_fields, plain, remux, samples, select_points, series, source_snapshot)
from data_web_platform.raw_viewer import RawViewer


def recording(tmp_path):
    root = tmp_path / "my_bag-one"
    (root / "data").mkdir(parents=True)
    (root / "data/record.mcap").write_bytes(b"mcap placeholder")
    (root / "data/metadata.yaml").write_text(yaml.safe_dump({"rosbag2_bagfile_information": {
        "relative_file_paths": ["record.mcap"], "topics_with_message_count": [
            {"topic_metadata": {"name": "/empty_cmd", "type": "std_msgs/msg/Float32"}, "message_count": 0}]}}))
    (root / ".recording-session.json").write_text(json.dumps({"phase": "saved", "data_state": "closed"}))
    return root


def test_snapshot_preserves_empty_topics_and_changes_identity(tmp_path):
    root = recording(tmp_path)
    before = source_snapshot(root, [tmp_path])
    assert before["topics"]["/empty_cmd"]["declared_count"] == 0
    assert len(before["fingerprint"]) == 3
    (root / "data/record.mcap").write_bytes(b"changed")
    assert source_snapshot(root, [tmp_path])["id"] != before["id"]


@pytest.mark.parametrize("phase", ["recording", "starting", "saving"])
def test_active_recording_is_rejected(tmp_path, phase):
    root = recording(tmp_path)
    (root / ".recording-session.json").write_text(json.dumps({"phase": phase}))
    with pytest.raises(ValueError, match="尚未保存"):
        source_snapshot(root, [tmp_path])


def test_mcap_symlink_cannot_escape_its_recording(tmp_path):
    root = recording(tmp_path)
    external = tmp_path / "outside.mcap"
    external.write_bytes(b"outside")
    path = root / "data/record.mcap"
    path.unlink(); path.symlink_to(external)
    with pytest.raises(ValueError, match="对应录制"):
        source_snapshot(root, [tmp_path])


def test_raw_field_layout_units_nan_and_text_are_retained():
    msg = SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(sec=12, nanosec=34), frame_id="flange_R"),
                          data=[3.14, -7, float("nan")], status="hello")
    decoded = plain(msg)
    fields = numeric_fields(decoded)
    assert fields["data[0]"] == 3.14 and fields["data[1]"] == -7
    assert np.isnan(fields["data[2]"])
    assert decoded["status"] == "hello" and decoded["header"]["frame_id"] == "flange_R"
    assert all(not key.startswith("header") for key in fields)
    assert header_time(msg) == 12_000_000_034
    msg.header.stamp.nanosec = 1_000_000_000
    assert header_time(msg) == -1


def test_plot_reduction_keeps_short_spikes_real_times_and_gap_boundaries():
    times=np.arange(10000)/1000
    times[5000:] += .7
    values=np.zeros((len(times),2)); values[1234,0]=100;values[4321,1]=-200
    selected, reduced, changed = select_points(times, values, 0, 11, 300)
    assert changed and len(selected)<len(times)
    assert 1.234 in selected and 4.321 in selected
    assert times[4999] in selected and times[5000] in selected
    assert set(selected) <= set(times)
    for t,v in zip(selected,reduced): np.testing.assert_array_equal(v, values[np.where(times==t)[0][0]])


def test_clock_summary_exposes_header_reset_and_duplicate():
    result=clock_info(np.array([100,200,200,150,300],dtype=np.int64),100)
    assert result["duplicates"] == 1 and result["reversals"] == 1
    assert result["start"] == 0 and result["end"] == 2e-7


def test_series_gap_flags_use_original_sampling_not_plot_decimation(tmp_path):
    topic={"id":"topic-000","fields":["x"],"count":10000,"clocks":{"record":{"gap_threshold":.1}}}
    manifest={"topics":[topic],"clocks":{"record":{"origin_ns":"0"}}}
    times=np.arange(10000,dtype=np.int64)*1_000_000
    times[5000:]+=700_000_000
    np.savez(tmp_path / "topic-000.npz",record=times,values=np.zeros((10000,1)))
    result=series(tmp_path,manifest,'topic-000',['x'],'record',0,11,100)
    assert result['reduced'] and sum(result['breaks'])==2
    assert result['times'][result['breaks'].index(True,1)]==5.7


def test_series_keeps_raw_values_and_queries_full_messages_at_cursor(tmp_path):
    topic={"id":"topic-000", "name":"/cmd", "fields":["data"], "count":4,
           "clocks":{"record":{"gap_threshold":.1},"header":{"gap_threshold":.1}}}
    manifest={"topics":[topic],"clocks":{"record":{"origin_ns":"1000000000"},"header":{"origin_ns":"900000000"}}}
    times=np.array([1_000_000_000,1_100_000_000,1_600_000_000,1_700_000_000])
    np.savez(tmp_path / "topic-000.npz",record=times,header=times-100_000_000,values=np.array([[7.],[8.],[9.],[10.]]))
    result=series(tmp_path,manifest,"topic-000",["data"],"record",0,1,200)
    assert result["times"] == [0,.1,.6,.7] and result["values"] == [[7],[8],[9],[10]]
    with sqlite3.connect(tmp_path / "messages.sqlite") as con:
        con.execute("CREATE TABLE messages(topic TEXT,seq INT,record_ns INT,header_ns INT,payload TEXT)")
        con.execute("INSERT INTO messages VALUES(?,?,?,?,?)",('/cmd',0,1_100_000_000,1_000_000_000,'{"data":8}'))
    topic["clocks"]["record"]["end"] = .7
    assert samples(tmp_path,manifest,["topic-000"],"record",.05)[0]["sample"] is None
    sample=samples(tmp_path,manifest,["topic-000"],"record",.5)[0]
    assert sample["sample"] == {"data":8} and sample["age_ms"] == 400


def test_remux_preserves_all_decoded_pixels_and_variable_timestamps(tmp_path):
    raw=tmp_path / "input.h264"
    with av.open(str(raw),"w",format="h264") as container:
        stream=container.add_stream("libx264",rate=30)
        stream.width,stream.height,stream.pix_fmt=64,48,"yuv420p"
        stream.options={"crf":"18","bf":"0","preset":"veryfast"}
        for i in range(5):
            frame=av.VideoFrame.from_ndarray(np.full((48,64,3),30+i*35,np.uint8),format="rgb24")
            for packet in stream.encode(frame):container.mux(packet)
        for packet in stream.encode(None):container.mux(packet)
    with av.open(str(raw)) as container:
        expected=[f.to_ndarray(format="rgb24") for f in container.decode(video=0)]
    times=np.array([0,33_333_333,66_666_666,400_000_000,433_333_333],dtype=np.int64)+1_700_000_000_000_000_000
    result=remux(raw,tmp_path / "preview.mp4",times)
    assert result=={"width":64,"height":48,"frames":5}
    with av.open(str(tmp_path / "preview.mp4")) as container:
        for i,frame in enumerate(container.decode(video=0)):
            np.testing.assert_array_equal(frame.to_ndarray(format="rgb24"),expected[i])
            assert abs(frame.pts*frame.time_base-Fraction(int(times[i]-times[0]),1_000_000_000))<Fraction(1,1_000_000)
    with pytest.raises(ValueError,match="重复或回退"):
        remux(raw,tmp_path / "bad.mp4",np.array([1,1,2,3,4]))


def test_cached_api_checks_path_freshness_bounds_and_http_ranges(tmp_path):
    root=recording(tmp_path)
    config=Config(project_root=tmp_path,state_dir=tmp_path / "state",allowed_roots=[tmp_path])
    snapshot=source_snapshot(root,[tmp_path]); token=snapshot["id"]
    directory=config.state_dir / "raw-viewer" / token;directory.mkdir(parents=True)
    manifest={"format":FORMAT,"id":token,"path":str(root),"topics":[],"video":{"record":{"file":"quad-record.mp4"}},"clocks":{"record":{"duration":1}}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "quad-record.mp4").write_bytes(b"0123456789")
    with patch("data_web_platform.app.discover_providers",return_value=[]):
        with TestClient(create_app(config,start_services=False)) as client:
            assert client.post("/api/raw/open",json={"path":str(root)}).json()["state"] == "ready"
            assert client.get(f"/api/raw/{token}/video/record",headers={"Range":"bytes=2-5"}).content == b"2345"
            assert client.get(f"/api/raw/{token}/video/bogus").status_code==404
            assert client.get(f"/api/raw/{token}/samples",params={"topics":"bogus","at":"nan"}).status_code==400
            assert client.post('/api/raw/open',json={'path':str(root)},headers={'Origin':'https://evil.example'}).status_code==403
            assert client.get('/api/raw/not-a-token').status_code==404
            (root / "data/record.mcap").write_bytes(b"changed")
            assert client.get(f"/api/raw/{token}").status_code==409


def test_camera_mapping_matches_recorded_quad_contract():
    assert [(c['name'],c['column'],c['row']) for c in CAMERAS] == [
        ('head_left',0,0),('head_right',1,0),('left_wrist',0,1),('right_wrist',1,1)]


def test_queue_is_bounded_deduplicated_and_incomplete_cache_is_preserved(tmp_path, monkeypatch):
    config=Config(project_root=tmp_path,state_dir=tmp_path/'state',allowed_roots=[tmp_path])
    service=RawViewer(config)
    submitted=[]
    monkeypatch.setattr(service.executor,'submit',lambda function,token:submitted.append(token))
    roots=[recording(tmp_path/str(i)) for i in range(5)]
    first=service.open(roots[0])
    assert service.open(roots[0])['id']==first['id'] and len(submitted)==1
    for root in roots[1:4]:assert service.open(root)['state']=='queued'
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:service.open(roots[4])
    assert caught.value.status_code==429
    directory=service.root/first['id']
    (directory/'partial.mp4').write_bytes(b'preserve this diagnostic')
    service.pending.remove(first['id'])
    assert service.status(first['id'])['state']=='interrupted'
    service.open(roots[0])
    diagnostic=list(service.root.glob(first['id'][:12]+'.incomplete-*/cache/partial.mp4'))
    assert len(diagnostic)==1 and diagnostic[0].read_bytes()==b'preserve this diagnostic'
    assert not (directory/'partial.mp4').exists()
    service.close()


def test_worker_source_change_never_publishes_index(tmp_path, monkeypatch):
    from data_web_platform import raw_worker
    root=recording(tmp_path)
    snapshot=source_snapshot(root,[tmp_path]); cache=tmp_path/'cache';cache.mkdir()
    request=cache/'request.json';request.write_text(json.dumps({'snapshot':snapshot,'roots':[str(tmp_path)]}))
    def changed(*args):
        (root/'data/record.mcap').write_bytes(b'changed after reading')
        return {'format':FORMAT}
    monkeypatch.setattr(raw_worker,'build_cache',changed)
    monkeypatch.setattr(raw_worker.os,'nice',lambda _:None)
    assert raw_worker.run(request)==1
    assert not (cache/'manifest.json').exists()
    assert json.loads((cache/'status.json').read_text())['state']=='failed'

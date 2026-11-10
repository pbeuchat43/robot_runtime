"""Bounded native recorder acceptance with synthetic ROS captures, no camera."""
# Copyright (c) 2026 Paul N. Beuchat. MIT License.
import argparse
import array
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid
import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rgbd_camera_interfaces.msg import Frame
from rgbd_object_detector_interfaces.msg import OverlayFrame


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scenario',choices=['recovery','index-off','interrupt','disk-failure','rate-limit'],default='recovery')
    args=parser.parse_args()
    assert os.environ.get('ROS_DOMAIN_ID')=='231'
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    run=uuid.uuid4().hex
    config=dict(schema_version=1,root=str(args.output/'history'),outputs=['rgb','depth','annotated'],frame_index=True,
        fps=30,segment_seconds=2,budget_bytes=1000000000,reserve_bytes=1000000000,min_depth_m=.25,max_depth_m=15,
        bitrates=dict(rgb=1000000,depth=1000000,annotated=1000000),source=dict(backend='oak',source_id='oak:fixture',width=1280,height=720,
            frame_topic='/recording_fixture/frames',overlay_topic='/recording_fixture/overlay',overlay_kind='single'))
    if args.scenario!='recovery':
        config['outputs']=['rgb'];config['frame_index']=args.scenario!='index-off'
    if args.scenario=='rate-limit':config['fps']=5
    (args.output/'config.json').write_text(json.dumps(config))
    rclpy.init();node=rclpy.create_node('recording_fixture')
    qos=QoSProfile(depth=1,reliability=ReliabilityPolicy.BEST_EFFORT)
    camera=node.create_publisher(Frame,'/recording_fixture/frames',qos)
    overlay=node.create_publisher(OverlayFrame,'/recording_fixture/overlay',qos)
    log=(args.output/'worker.log').open('w')
    process=subprocess.Popen([args.executable,'--ros-args','-p','config_file:='+str(args.output/'config.json'),
        '-p','run_id:='+run,'-p','status_file:='+str(args.output/'status.json')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    first_generation=str(uuid.uuid4());generation=first_generation
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip();offset=time.time_ns()-time.monotonic_ns()
    states=[];published=[];sequence=0;fault_injected=False;session=None
    frame=Frame();frame.rgb.width=frame.depth.width=1280;frame.rgb.height=frame.depth.height=720
    frame.rgb.encoding='bgr8';frame.rgb.step=3840;frame.depth.encoding='16UC1';frame.depth.step=2560
    frame.depth.data=array.array('B',b'\xe8\x03'*(1280*720))
    for info in (frame.rgb_info,frame.depth_info):info.width=1280;info.height=720;info.k=[600.,0.,640.,0.,600.,360.,0.,0.,1.];info.d=[0.]*5
    frame.alignment='aligned_to_rgb';frame.depth_convention='optical_z';frame.depth_unit_m=.001
    frame.calibration_id='a'*64;frame.backend='synthetic';frame.pairing_tolerance_ns=5000000
    frame.capture.source_id='oak:fixture';frame.capture.stream_id='rgbd';frame.capture.clock_domain='linux_monotonic:'+boot
    frame.capture.unix_offset_ns=offset
    try:
        deadline=time.monotonic()+10
        while camera.get_subscription_count()==0 and time.monotonic()<deadline:rclpy.spin_once(node,timeout_sec=.05)
        assert camera.get_subscription_count()>0
        started=time.monotonic();next_frame=started
        while (elapsed:=time.monotonic()-started)<(16 if args.scenario=='recovery' else 8):
            if args.scenario=='disk-failure' and process.poll() is not None:break
            assert process.poll() is None,'native worker exited early'
            if args.scenario=='recovery' and 5<elapsed<8: # Whole source outage.
                time.sleep(.05)
            elif time.monotonic()>=next_frame:
                if args.scenario=='recovery' and elapsed>=8:generation='second-'+first_generation
                frame.calibration_id=('e' if generation!=first_generation else 'a')*64
                for info in (frame.rgb_info,frame.depth_info):
                    info.k[0]=info.k[4]=605. if generation!=first_generation else 600.
                sequence+=1;now=time.monotonic_ns();frame.capture.generation=generation;frame.capture.sequence=sequence
                frame.capture.acquisition_ns=now-2000000;frame.capture.received_ns=now;frame.depth_acquisition_ns=now-1000000
                frame.depth_sequence=sequence+1000;frame.skew_ns=1000000
                for image,info,stamp in ((frame.rgb,frame.rgb_info,frame.capture.acquisition_ns+offset),(frame.depth,frame.depth_info,frame.depth_acquisition_ns+offset)):
                    image.header.stamp.sec=stamp//1000000000;image.header.stamp.nanosec=stamp%1000000000;image.header.frame_id='fixture_optical'
                    info.header=image.header
                frame.rgb.data=array.array('B',bytes([40+sequence%128,80,120])*(1280*720))
                # Large binary tiles survive lossy H.264, unlike one-level colour
                # increments. They make the decoded capture sequence testable.
                rgb_marker=b''.join(bytes([235 if sequence&(1<<bit) else 16])*96 for bit in range(16))
                rgb_row=rgb_marker+bytes([40+sequence%128,80,120])*(1280-512)
                frame.rgb.data[:3840*32]=array.array('B',rgb_row*32)
                depth_marker=b''.join((b'\xe8\x03' if sequence&(1<<bit) else b'\0\0')*32 for bit in range(16))
                frame.depth.data[:2560*32]=array.array('B',(depth_marker+b'\xe8\x03'*(1280-512))*32)
                # Invalid identities use fresh timestamps, proving admission rejects
                # them for identity rather than only out-of-order delivery.
                invalid=args.scenario=='recovery' and (sequence%19==0 or (elapsed>9 and sequence%19==1))
                if invalid:
                    if sequence%19==1:frame.capture.generation=first_generation
                    elif sequence%38==0:frame.capture.source_id='oak:other'
                    else:frame.capture.clock_domain='linux_monotonic:wrong-boot'
                camera.publish(frame)
                if not invalid:published.append(dict(generation=generation,sequence=str(sequence)))
                # Independent annotated outage after resumption; RGB/depth keep running.
                if not invalid and not (args.scenario=='recovery' and 10<elapsed<13):
                    message=OverlayFrame();message.image=frame.rgb;d=message.capture_detections
                    d.capture=frame.capture;d.calibration_id=frame.calibration_id;d.source_width=1280;d.source_height=720
                    d.model_id='synthetic';d.model_sha256='b'*64;d.model_profile_sha256='c'*64;d.engine_sha256='d'*64
                    d.depth_sequence=frame.depth_sequence;d.depth_acquisition_ns=frame.depth_acquisition_ns
                    overlay.publish(message)
                frame.capture.source_id='oak:fixture';frame.capture.clock_domain='linux_monotonic:'+boot
                next_frame=time.monotonic()+.05
            rclpy.spin_once(node,timeout_sec=0)
            try:
                state=json.loads((args.output/'status.json').read_text());states.append((state['state'],{k:v['state'] for k,v in state['outputs'].items()}))
                session=Path(state['output_directory'])
                if args.scenario=='disk-failure' and not fault_injected and int(state['outputs']['rgb']['counters']['finalized'])>0:
                    session.chmod(0o500);fault_injected=True
            except FileNotFoundError:pass
            time.sleep(.002)
        if process.poll() is None:os.killpg(process.pid,signal.SIGKILL if args.scenario=='interrupt' else signal.SIGINT)
        process.wait(timeout=18)
        if session:session.chmod(0o700)
        state=json.loads((args.output/'status.json').read_text())
        session=Path(state['output_directory'])
        if args.scenario in ('interrupt','disk-failure'):
            assert process.returncode!=0,(args.scenario,state)
            assert (session/'rgb-segments.jsonl').is_file(),'no previously completed segment preserved'
            assert json.loads((session/'manifest.json').read_text())['state']!='completed'
            result=dict(passed=True,scenario=args.scenario,session=str(session),returncode=process.returncode,final_state=state)
            (args.output/'report.json').write_text(json.dumps(result,indent=2));print(json.dumps(result));return
        assert process.returncode==0,state
        assert state['state']=='completed' and state['finalization']=='complete'
        if args.scenario=='recovery':
            assert any(s[0]=='waiting_for_source' for s in states)
            assert any(s[1].get('annotated')=='waiting_for_source' and s[1].get('rgb')=='recording' for s in states)
        elif args.scenario=='index-off':
            assert not (session/'rgb.jsonl').exists()
            assert not json.loads((session/'manifest.json').read_text())['exact_frame_reconstruction']
        session=Path(state['output_directory']);keys={(x['generation'],x['sequence']) for x in published}
        for name,output in state['outputs'].items():
            if not config['frame_index']:
                assert int(output['counters']['encoded'])==int(output['counters']['finalized'])>80
                continue
            rows=[json.loads(x) for x in (session/(name+'.jsonl')).read_text().splitlines()]
            assert len(rows)==int(output['counters']['encoded'])==int(output['counters']['finalized'])>(25 if args.scenario=='rate-limit' else 80)
            assert len({row['capture']['generation'] for row in rows})==(2 if args.scenario=='recovery' else 1)
            assert all((row['capture']['generation'],row['capture']['sequence']) in keys for row in rows)
            by_file={}
            for row in rows:by_file.setdefault(row['segment'],set()).add(row['capture']['generation'])
            assert all(len(values)==1 for values in by_file.values()),'generations spliced in one segment'
        if args.scenario=='recovery':
            assert int(state['outputs']['rgb']['counters']['rejected'])>0
            generations=[json.loads(line) for line in (session/'events.jsonl').read_text().splitlines() if json.loads(line)['event']=='generation']
            assert [event['calibration_id'] for event in generations]==['a'*64,'e'*64]
            assert [event['geometry']['k'][0] for event in generations]==[600.,605.]
        if args.scenario=='rate-limit':assert int(state['outputs']['rgb']['counters']['rate_dropped'])>0
        result=dict(passed=True,kind='synthetic ROS input and native hardware encoding; no camera',session=str(session),outputs=state['outputs'])
        (args.output/'report.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
    finally:
        if session:session.chmod(0o700)
        if process.poll() is None:os.killpg(process.pid,signal.SIGINT);process.wait(timeout=18)
        log.close();node.destroy_node();rclpy.shutdown()


if __name__=='__main__':main()

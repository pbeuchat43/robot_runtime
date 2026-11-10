"""Recorder admission regressions; run on the reserved Linux host."""
# Copyright (c) 2026 Paul N. Beuchat. MIT License.
import copy
import hashlib
import json
import subprocess
import sys
import unittest
from robot_runtime import prepared, recording
from robot_runtime.core import Core
from robot_runtime.profile import validate
import test_core


class RecordingTests(unittest.TestCase):
    tearDown = test_core.PolicyTests.tearDown
    command = test_core.PolicyTests.command
    assertFault = test_core.PolicyTests.assertFault
    def setUp(self):
        test_core.PolicyTests.setUp(self)
        config = test_core.profile()
        source = config["units"][0]
        source["launch_modes"] = [dict(id="camera", label="Camera", argv=source["argv"]), dict(id="both", label="Both", argv=source["argv"])]
        recorder = copy.deepcopy(source)
        recorder.update(id="recording", label="Recording", requires_running=["worker"], preparation=dict(argv=["/usr/bin/true"], timeout_seconds=1))
        recorder.pop("launch_modes")
        config["units"].append(recorder)
        config["recording"] = dict(unit_id="recording", source_unit_id="worker", overlays_by_mode=dict(camera=[], both=["cones","coco","combined"]))
        self.config, self.revision = validate(config)
        self.core = Core(self.config,self.revision,self.store,"boot-one",clock=self.clock,wall=self.clock.wall)
        self.core.connected(self.supervisor)

    def test_profile_validation_does_not_require_native_supervisor(self):
        result = subprocess.run([sys.executable, "-c",
            "import json,sys; sys.modules['dbus']=None; "
            "from robot_runtime.profile import validate; validate(json.load(sys.stdin))"],
            input=json.dumps(self.config), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def start_source(self, mode="both"):
        path = self.path / "source.json"; raw=b'{}'; path.write_bytes(raw)
        candidate=dict(schema_version=1,unit_id="worker",config_revision=self.revision,mode=mode,
            inputs=[dict(name="effective",path=str(path),sha256=hashlib.sha256(raw).hexdigest())])
        self.core.handle(self.command(mode=mode,prepared=candidate))
        return self.core.current("worker")

    def recording_command(self, **extra):
        value = self.command(**extra)
        value["unit_id"] = "recording"
        return value

    def candidate(self):
        path=self.path/"recording.json";raw=b'{}';path.write_bytes(raw)
        return dict(schema_version=1,unit_id="recording",config_revision=self.revision,mode=None,
            inputs=[dict(name="recording",path=str(path),sha256=hashlib.sha256(raw).hexdigest())])

    def test_recording_source_change_between_preparation_and_commit(self):
        source=self.start_source();request=self.recording_command(recording=dict(outputs=["rgb","annotated"],overlay="combined"))
        target=self.core.preparation_target(request)
        self.supervisor.exit(source);self.core.audit()
        self.assertFault("precondition",lambda:self.core.handle(request,prepared_candidate=self.candidate(),preparation_binding=target["recording"]))
        self.assertEqual(self.supervisor.starts,1)

    def test_recording_freezes_and_duplicate_does_not_start_twice(self):
        self.start_source();request=self.recording_command(recording=dict(outputs=["depth","rgb"],frame_index=False))
        target=self.core.preparation_target(request)
        result=self.core.handle(request,prepared_candidate=self.candidate(),preparation_binding=target["recording"])
        self.assertEqual(self.core.handle(request),result)
        run=self.core.current("recording")
        self.assertEqual(prepared.load(run["prepared"])[1]["recording"],b'{}')
        self.assertEqual(self.supervisor.starts,2)
        self.assertEqual(run["recording_source"]["options"]["frame_index"],False)

    def test_recording_cannot_bypass_admission(self):
        self.start_source("camera")
        self.assertFault("invalid_request",lambda:self.core.preparation_target(self.recording_command(prepared=self.candidate())))
        self.assertFault("precondition",lambda:self.core.preparation_target(self.recording_command(recording=dict(outputs=["annotated"],overlay="cones"))))
        self.assertFault("invalid_request",lambda:self.core.handle(self.command(recording=dict(outputs=["rgb"])) ))
        self.assertFault("stale_source",lambda:self.core.handle(self.recording_command(),prepared_candidate=self.candidate()))

    def test_recording_observation_never_infers_finalization_from_process_exit(self):
        self.start_source();request=self.recording_command();target=self.core.preparation_target(request)
        self.core.handle(request,prepared_candidate=self.candidate(),preparation_binding=target["recording"])
        run=self.core.current("recording")
        self.assertEqual(recording.observation(self.core,run)["state"],"unknown")
        value=dict(run_id=run["run_id"],boot_id="boot-one",heartbeat_ns=str(int(self.clock()*1e9)),state="recording",finalization="pending")
        self.core.recording_observations[run["run_id"]]=value
        self.assertTrue(recording.observation(self.core,run)["fresh"])
        run["blocked"]=False
        self.assertEqual(recording.observation(self.core,run)["state"],"awaiting_final_status")
        self.assertFalse(recording.observation(self.core,run)["fresh"])
        run["blocked"]=True
        self.clock.now+=4
        self.assertEqual(recording.observation(self.core,run)["state"],"unknown")
        run["blocked"]=False
        self.assertEqual(recording.observation(self.core,run)["state"],"interrupted")
        value.update(state="completed",finalization="complete")
        self.assertEqual(recording.observation(self.core,run)["state"],"completed")


class OptionTests(unittest.TestCase):
    def test_invalid_combinations(self):
        for value in ({"outputs":[]},{"outputs":["rgb","rgb"]},{"outputs":["annotated"]},
                      {"outputs":["rgb"],"overlay":"cones"},{"outputs":["rgb"],"frame_index":1},
                      {"outputs":["rgb"],"root":"/tmp"}):
            with self.assertRaises(Exception):recording.options(value)
        self.assertTrue(recording.options({"outputs":["rgb"]})["frame_index"])

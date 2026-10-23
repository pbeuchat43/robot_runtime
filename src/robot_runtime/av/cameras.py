# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""RGB discovery and replaceable capture; independent of the WebRTC connection."""
import errno
from functools import partial
import multiprocessing
import os
import signal
from pathlib import Path
from queue import Empty, Full
import struct
import threading
import time
from .native_modes import enumerate_modes, choose_mode, intervals_for_size, source_caps
from .settings_config import load_profile

RGB_FORMATS = {'YUYV', 'UYVY', 'NV12', 'RGB3', 'BGR3', 'MJPG', 'JPEG'}


def is_rgb(formats, kind, interface=''):
    if not formats & RGB_FORMATS:
        return False
    if kind == 'realsense':
        # D455 infrared advertises UYVY too. Use the USB interface identity,
        # not just FourCC, to avoid presenting IR as a second colour camera.
        return 'rgb' in interface.lower() if interface else bool(formats & {'YUYV', 'RGB3', 'BGR3', 'MJPG'}) and not formats & {'GREY', 'Y8I ', 'Y12I'}
    return True


def video_formats(path):
    """Probe capture nodes without starting capture (metadata/depth are excluded)."""
    import fcntl
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        caps = bytearray(104)
        fcntl.ioctl(descriptor, 0x80685600, caps)  # VIDIOC_QUERYCAP
        capabilities, device_caps = struct.unpack_from('II', caps, 84)
        if not ((device_caps if capabilities & 0x80000000 else capabilities) & 1):
            return set()
        formats = set()
        for index in range(64):
            fmt = bytearray(64)
            struct.pack_into('II', fmt, 0, index, 1)  # VIDEO_CAPTURE
            try:
                fcntl.ioctl(descriptor, 0xc0405602, fmt)  # VIDIOC_ENUM_FMT
            except OSError as error:
                if error.errno == errno.EINVAL:
                    break
                raise
            formats.add(bytes(fmt[44:48]).decode('ascii', errors='replace'))
        return formats
    finally:
        os.close(descriptor)


class NativeOakCapture:
    def __init__(self, device, width, height, fps):
        import depthai as dai
        self.dai = dai
        self.device = dai.Device(dai.DeviceInfo(device['serial']))
        try:
            self.pipeline = dai.Pipeline(self.device)
            camera = self.pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
            output = camera.requestOutput((width, height), dai.ImgFrame.Type.NV12, dai.ImgResizeMode.CROP, fps)
            self.queue = output.createOutputQueue(maxSize=1, blocking=False)
            self.pipeline.start()
        except Exception:
            self.device.close()
            raise

    def read(self):
        if not self.pipeline.isRunning():
            raise RuntimeError('OAK pipeline stopped')
        frame = self.queue.tryGet()
        if frame is None:
            return None
        age = max(0, (self.dai.Clock.now() - frame.getTimestamp()).total_seconds())
        return frame.getData().copy(), time.monotonic() - age

    def close(self):
        try:
            self.pipeline.stop()
            self.pipeline.wait()
        finally:
            self.device.close()


def oak_worker(device, width, height, fps, pixels, stamp, sequence, lock, stopped, errors, ready):
    """Native SDK failures must not terminate HTTP, WebRTC or input receipt."""
    import numpy as np
    capture = None
    signalled = False
    def request_stop(*_):
        # A signal can interrupt Event.wait while its process-shared lock is
        # held. Never acquire that lock again from the Python signal handler.
        nonlocal signalled
        signalled = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        capture = NativeOakCapture(device, width, height, fps)
        target = np.frombuffer(pixels, dtype=np.uint8)
        while not signalled and not stopped.wait(.002):
            if signalled:
                break
            frame = capture.read()
            if frame is not None:
                data, at = frame
                with lock:
                    target[:] = data
                    stamp.value = at
                    sequence.value += 1
                    ready.set()
    except Exception as error:
        try:
            errors.put_nowait(str(error)[:2000])
        except Full:
            pass
    finally:
        if capture:
            capture.close()  # A native segfault here is confined to this child.


class OakCapture:
    startup_timeout = 15

    def __init__(self, device, width, height, fps, worker=oak_worker):
        context = multiprocessing.get_context('spawn')
        self.pixels = context.RawArray('B', width * height * 3 // 2)
        self.stamp, self.sequence = context.RawValue('d', 0), context.RawValue('Q', 0)
        self.lock, self.stopped, self.errors = context.Lock(), context.Event(), context.Queue(maxsize=1)
        self.ready = context.Event()
        self.seen = 0
        self.process = context.Process(target=worker, args=(device, width, height, fps, self.pixels,
            self.stamp, self.sequence, self.lock, self.stopped, self.errors, self.ready), daemon=False)
        self.process.start()

    def read(self):
        import numpy as np
        if not self.process.is_alive():
            try:
                error = self.errors.get_nowait()
            except Empty:
                error = f'OAK capture process exited ({self.process.exitcode})'
            raise RuntimeError(error)
        if not self.ready.wait(.1):
            return None
        # A child killed during its copy must not leave the parent waiting on
        # an abandoned lock. Only the latest complete frame is ever consumed.
        if not self.lock.acquire(timeout=.02):
            return None
        try:
            if self.sequence.value == self.seen:
                self.ready.clear()
                return None
            self.seen = self.sequence.value
            self.ready.clear()
            return np.frombuffer(self.pixels, dtype=np.uint8).copy(), self.stamp.value
        finally:
            self.lock.release()

    def close(self):
        self.stopped.set()
        self.process.join(timeout=5)
        if self.process.is_alive():
            raise RuntimeError('Camera capture did not stop gracefully; ownership is retained')
        self.errors.close()


class V4L2Capture:
    """D455 RGB and ordinary UVC cameras use the same colour capture adapter."""
    def __init__(self, device, width, height, fps, policy=None):
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstVideo', '1.0')
        from gi.repository import Gst, GstVideo
        self.Gst, self.GstVideo = Gst, GstVideo
        self.width, self.height = width, height
        policy = policy or load_profile()
        modes = device.get('modes')
        if modes is None:
            modes = enumerate_modes(device['path'], device['formats'])
        self.mode = choose_mode(modes, width, height, fps, policy['native_format_preference'],
            policy['capture_mode'], partial(intervals_for_size, device['path']))
        jpeg = self.mode['format'] in ('MJPG', 'JPEG')
        media = 'image/jpeg' if jpeg else 'video/x-raw'
        self.pipeline = Gst.parse_launch(
            f'v4l2src name=camera do-timestamp=true ! {source_caps(self.mode)} ! '
            'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            f'videorate drop-only=true ! {media},framerate={fps}/1 ! '
            + ('jpegdec ! ' if jpeg else '') + 'videoconvert ! videoscale ! '
            f'video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1 ! '
            'appsink name=frames max-buffers=1 drop=true sync=false enable-last-sample=false')
        self.pipeline.get_by_name('camera').set_property('device', device['path'])
        self.sink = self.pipeline.get_by_name('frames')
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError('Could not start RGB capture')

    def read(self):
        import numpy as np
        Gst = self.Gst
        error = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if error:
            raise RuntimeError(str(error.parse_error()[0]) if error.type == Gst.MessageType.ERROR else 'Camera ended')
        sample = self.sink.emit('try-pull-sample', 20 * Gst.MSECOND)
        if sample is None:
            return None
        buffer = sample.get_buffer()
        info = self.GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        raw = np.frombuffer(buffer.extract_dup(0, buffer.get_size()), dtype=np.uint8)
        # NV12 can have per-plane alignment padding; never pass it into appsrc.
        planes = [raw[info.offset[i]:info.offset[i] + info.stride[i] * rows].reshape(rows, info.stride[i])[:, :self.width]
                  for i, rows in ((0, self.height), (1, self.height // 2))]
        age = 0
        if buffer.pts != Gst.CLOCK_TIME_NONE:
            now = self.pipeline.get_clock().get_time() - self.pipeline.get_base_time()
            age = max(0, (now - buffer.pts) / Gst.SECOND)
        return np.concatenate([plane.ravel() for plane in planes]), time.monotonic() - age

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)


class TestCapture:
    def __init__(self, device, width, height, fps):
        import numpy as np
        self.np, self.width, self.height, self.fps = np, width, height, fps
        self.texture = np.random.default_rng(20261014).integers(16, 236, (height, width), dtype=np.uint8)
        self.index, self.next_at = 0, 0
        self.stopped = threading.Event()

    def read(self):
        if self.stopped.wait(max(0, self.next_at - time.monotonic())):
            return None
        now = time.monotonic()
        self.next_at = now + 1 / self.fps
        data = self.np.full(self.width * self.height * 3 // 2, 128, dtype=self.np.uint8)
        data[:self.width * self.height] = self.np.roll(self.texture, self.index * 5, axis=1).ravel()
        self.index += 1
        return data, now

    def close(self):
        self.stopped.set()


def open_camera(device, width, height, fps, policy=None):
    if device['kind'] == 'ros':
        from .ros_source import RosCapture
        return RosCapture(device, width, height, fps, policy or load_profile())
    factory = {'oak': OakCapture, 'test': TestCapture, 'webcam': V4L2Capture, 'realsense': V4L2Capture}[device['kind']]
    if factory is V4L2Capture:
        return factory(device, width, height, fps, policy)
    return factory(device, width, height, fps)


class Cameras:
    def __init__(self, source, width, height, fps, discover_fn=None, open_fn=open_camera, policy=None, active=True):
        self.preferred, self.width, self.height, self.fps = source, width, height, fps
        self.policy = policy or load_profile()
        self.active = active
        if discover_fn is None:
            raise ValueError('Runtime cameras require a declared source')
        self.discover_fn = discover_fn
        self.open_fn = partial(open_camera, policy=self.policy) if open_fn is open_camera else open_fn
        self.lock, self.stopped = threading.RLock(), threading.Event()
        self.changed = threading.Condition(self.lock)
        self.devices, self.warnings, self.selected = [], [], None
        self.selected_device = None
        self.capture_mode = None
        self.refresh_requested = threading.Event()
        self.refreshing, self.discovery_error = False, None
        self.last_discovery_ms, self.discovery_scans = None, 0
        self.state, self.error, self.latest = 'discovering', None, None
        self.revision, self.frame_number, self.reconnects = 0, 0, 0
        self.threads = []

    def refresh(self):
        devices, warnings = self.discover_fn()
        with self.lock:
            # Some SDKs omit an already open device from discovery.
            current = self.selected_device
            if current and self.state == 'streaming' and not any(d['id'] == self.selected for d in devices):
                devices.append(current.copy())
            self.devices, self.warnings = devices, warnings
            current = next((d for d in devices if d['id'] == self.selected), None)
            if current:
                self.selected_device = current.copy()
            if self.selected is None:
                match = next((d for d in devices if d['id'] == self.preferred), None)
                match = match or next((d for d in devices if d['kind'] == self.preferred or
                    (self.preferred == 'auto' and d['kind'] not in ('test', 'ros'))), None)
                if match:
                    self.select(match['id'])

    def select(self, identity):
        with self.lock:
            device = next((d for d in self.devices if d['id'] == identity), None)
            if device is None:
                raise ValueError('Camera is not in the discovered device list')
            if identity != self.selected:
                self.selected, self.latest, self.error = identity, None, None
                self.selected_device = device.copy()
                self.revision += 1
                self.state = 'opening' if self.active else 'idle'
                self.capture_mode = None
                self.changed.notify_all()

    def set_active(self, active):
        with self.changed:
            if self.active != active:
                self.active = active
                self.latest, self.error, self.capture_mode = None, None, None
                self.revision += 1
                self.state = ('opening' if self.selected else 'waiting') if active else 'idle'
                self.changed.notify_all()

    def request_refresh(self):
        """Queue one scan; requests during that scan share its result."""
        with self.lock:
            if self.stopped.is_set():
                raise ValueError('Camera discovery is closed')
            if self.refreshing:
                return False
            self.refreshing, self.discovery_error = True, None
            self.refresh_requested.set()
            return True

    def configure(self, width, height, fps, policy=None):
        """Reopen only the chosen camera; retain discovery and recovery identity."""
        with self.lock:
            age_changed = policy is not None and policy['max_source_age_ms'] != self.policy['max_source_age_ms']
            if policy is not None:
                self.policy.update(policy)
            if (width, height, fps) == (self.width, self.height, self.fps) and not age_changed:
                return False
            self.width, self.height, self.fps = width, height, fps
            self.latest, self.error = None, None
            self.revision += 1
            self.state = ('opening' if self.selected else 'waiting') if self.active else 'idle'
            self.capture_mode = None
            self.changed.notify_all()
            return True

    def snapshot(self, include_modes=False):
        with self.lock:
            devices = [dict(device) if include_modes else {k: v for k, v in device.items() if k != 'modes'}
                       for device in self.devices]
            return {'devices': devices, 'selected': self.selected, 'state': self.state,
                    'active': self.active, 'capture_mode': self.capture_mode,
                    'error': self.error, 'warnings': self.warnings.copy(), 'revision': self.revision,
                    'discovery': {'refreshing': self.refreshing, 'last_success_ms': self.last_discovery_ms,
                                  'error': self.discovery_error, 'scans': self.discovery_scans},
                    'frames': self.frame_number, 'reconnects': self.reconnects,
                    'output': {'width': self.width, 'height': self.height, 'fps': self.fps},
                    'frame_age_ms': (time.monotonic() - self.latest[2]) * 1000 if self.latest else None}

    def frame(self):
        with self.lock:
            return self.latest

    def wait_for_frame(self, after, timeout):
        with self.changed:
            self.changed.wait_for(lambda: self.stopped.is_set() or
                                 (self.latest is not None and self.latest[0] != after), timeout)
            return self.latest

    def start(self):
        def discovery_loop():
            while not self.stopped.is_set():
                self.refresh_requested.wait()
                self.refresh_requested.clear()
                if self.stopped.is_set():
                    break
                try:
                    self.refresh()
                except Exception as error:
                    with self.lock:
                        self.discovery_error = str(error)
                else:
                    with self.lock:
                        self.last_discovery_ms = time.time() * 1000
                finally:
                    with self.lock:
                        self.discovery_scans += 1
                        self.refreshing = False
        self.request_refresh()
        self.threads = [threading.Thread(target=discovery_loop),
                        threading.Thread(target=self.capture_loop)]
        for thread in self.threads:
            thread.start()

    def capture_loop(self):
        while not self.stopped.is_set():
            capture = None
            with self.changed:
                self.changed.wait_for(lambda: self.stopped.is_set() or (self.active and self.selected is not None))
                if self.stopped.is_set():
                    break
                identity, revision = self.selected, self.revision
                width, height, fps = self.width, self.height, self.fps
                # Keep retrying the chosen device even if a manual scan while
                # unplugged removed it from the available-camera inventory.
                device = self.selected_device
                if identity is None:
                    self.state = 'waiting'
            try:
                if device is None:
                    raise RuntimeError('Selected camera is disconnected')
                capture = self.open_fn(device, width, height, fps)
                last_frame = time.monotonic()
                timeout = getattr(capture, 'startup_timeout', self.policy['camera_stall_timeout_s'])
                while not self.stopped.is_set() and revision == self.revision:
                    frame = capture.read()
                    if frame is not None:
                        data, at = frame
                        with self.lock:
                            if revision != self.revision:
                                break
                            self.frame_number += 1
                            self.latest = (self.frame_number, data, at)
                            if self.state != 'streaming' and not any(d['id'] == identity for d in self.devices):
                                self.devices.append(device.copy())
                            self.state, self.error = 'streaming', None
                            self.capture_mode = getattr(capture, 'mode', None)
                            self.changed.notify_all()
                        last_frame = time.monotonic()
                        timeout = self.policy['camera_stall_timeout_s']
                    elif time.monotonic() - last_frame > timeout:
                        raise RuntimeError(getattr(capture, 'timeout_message', 'Camera stopped producing RGB frames'))
            except Exception as error:
                with self.lock:
                    if revision == self.revision:
                        self.state, self.error, self.latest = 'unavailable', str(error), None
                        self.reconnects += 1
                        self.changed.notify_all()
                print(f'Camera {identity}: {error}; retrying', flush=True)
            finally:
                if capture:
                    try:
                        capture.close()
                    except Exception as error:
                        with self.lock:
                            self.blocked_capture = capture
                            self.state, self.error, self.latest = 'stop_failed', str(error), None
                            self.stopped.set()
                            self.changed.notify_all()
                        print(f'Camera ownership retained: {error}', flush=True)
                        return
            if revision == self.revision:
                with self.changed:
                    self.changed.wait_for(lambda: self.stopped.is_set() or revision != self.revision,
                                          self.policy['camera_retry_interval_s'])

    def close(self):
        self.stopped.set()
        with self.changed:
            self.changed.notify_all()
        self.refresh_requested.set()
        for thread in self.threads:
            thread.join(timeout=6)
            if thread.is_alive():
                raise RuntimeError('Camera worker did not stop gracefully; inspect its runtime cgroup')
        if getattr(self, 'blocked_capture', None):
            self.blocked_capture.close()

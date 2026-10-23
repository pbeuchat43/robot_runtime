# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat
"""Camera-to-WebRTC media service; input transport has a separate lifetime."""
import argparse
import json
import resource
import threading
import time
from .gst_runtime import Gst, GLib, restrict_ice
from .media import stamp_nv12
from .diagnostics import JsonLog
from .stream_settings import StreamSettings, SettingsConflict, settings_from_args


def structure_json(value):
    if isinstance(value, Gst.Structure):
        return {value.nth_field_name(i): structure_json(value.get_value(value.nth_field_name(i)))
                for i in range(value.n_fields())}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple, Gst.ValueArray)):
        return [structure_json(item) for item in value]
    return str(value)


class MediaStream:
    def __init__(self, args, output, cameras, signalling_uri, signalling_secret):
        self.args, self.output = args, output
        self.stopped = threading.Event()
        self.feed_stopped = threading.Event()
        self.lock = threading.RLock()
        self.signalling_uri, self.signalling_secret = signalling_uri, signalling_secret
        self.frames = 0
        self.source_old_frames = 0
        self.source_queue_skips = 0
        self.source_age_ms = None
        self.source_luma = None
        self.source_error = None
        self.consumers = {}
        self.media_ports = {}
        self.media_sessions = {}
        self.ice_agents = {}
        self.full_diagnostics = args.diagnostics == 'full'
        self.cameras = cameras
        self.last_consumer_at = time.monotonic()
        self.settings = StreamSettings(settings_from_args(args), self.apply_settings, self.record_settings)
        self.encoders = {}
        self.phase = 'manual'
        self.measuring = False
        self.latest_browser = None
        self.last_browser_at = 0
        self.log = JsonLog(output / 'host.jsonl', self.full_diagnostics, args.log_queue_size)
        self.browser_log = JsonLog(output / 'browser.jsonl', self.full_diagnostics, args.log_queue_size)
        self.build_pipeline()

    def build_pipeline(self):
        args = self.args
        self.pipeline_handlers = []
        self.pipeline = Gst.Pipeline.new('robot-adaptive-camera')
        self.source = Gst.ElementFactory.make('appsrc', 'latest-camera-frame')
        for key, value in [('is-live', True), ('format', Gst.Format.TIME), ('block', False),
                           ('max-buffers', 1), ('max-bytes', 0), ('max-time', 0), ('leaky-type', 2)]:
            self.source.set_property(key, value)
        self.source.set_property('caps', Gst.Caps.from_string(
            f'video/x-raw,format=NV12,width={args.width},height={args.height},framerate={args.fps}/1'))
        self.queue = Gst.ElementFactory.make('queue', 'fresh-frame-queue')
        for key, value in [('max-size-buffers', 1), ('max-size-bytes', 0), ('max-size-time', 0), ('leaky', 2)]:
            self.queue.set_property(key, value)
        self.sink = Gst.ElementFactory.make('webrtcsink', 'adaptive-stream')
        missing = [name for name in ('webrtcsink', 'rtpgccbwe', 'nicesink') if not Gst.ElementFactory.find(name)]
        if not self.sink or missing:
            raise RuntimeError('Missing native GStreamer elements: ' + ', '.join(missing) + '; provision the unit native prefix')
        self.sink.set_property('video-caps', Gst.Caps.from_string('video/x-h264,profile=constrained-baseline'))
        self.sink.set_property('stun-server', None)
        # The default TURN list is empty; PyGObject cannot marshal a Python list
        # into this plugin's GstValueArray property on Ubuntu 24.04.
        self.sink.set_property('congestion-control', 2 if args.mode == 'adaptive' else 0)
        self.sink.set_property('min-bitrate', args.min_bitrate)
        self.sink.set_property('start-bitrate', args.bitrate)
        # Disabled congestion control uses max-bitrate, not start-bitrate.
        self.sink.set_property('max-bitrate', args.bitrate if args.mode == 'fixed' else args.max_bitrate)
        Gst.util_set_object_arg(self.sink, 'enable-mitigation-modes', 'downscaled')
        self.sink.set_property('do-retransmission', args.recovery)
        self.sink.set_property('do-fec', args.recovery)
        self.sink.set_property('run-signalling-server', False)
        self.sink.set_property('enable-control-data-channel', False)
        self.sink.set_property('enable-data-channel-navigation', False)
        self.sink.set_property('meta', Gst.Structure.new_from_string('meta,name=Robot-Camera'))
        # Only our admitted signalling service can request native sessions.
        self.signaller = self.sink.get_property('signaller')
        self.pipeline_handlers.append((self.signaller, self.signaller.connect('session-requested', self.session_requested)))
        self.signaller.set_property('uri', self.signalling_uri)
        headers = Gst.Structure.new_empty('headers')
        headers.set_value('Authorization', 'Bearer ' + self.signalling_secret)
        self.signaller.set_property('headers', headers)
        self.pipeline_handlers.append((self.signaller, self.signaller.connect(
            'error', lambda _, error: print('Signalling:', error, flush=True))))
        for name, handler in [('consumer-added', self.consumer_added), ('consumer-removed', self.consumer_removed),
                              ('encoder-setup', self.encoder_setup), ('payloader-setup', self.payloader_setup)]:
            self.pipeline_handlers.append((self.sink, self.sink.connect(name, handler)))
        for element in [self.source, self.queue, self.sink]:
            self.pipeline.add(element)
        if not self.source.link(self.queue) or not self.queue.link(self.sink):
            raise RuntimeError('Cannot link raw camera to WebRTC sink')

    def end_session(self, identity):
        self.signaller.emit('end-session', identity)
        return False  # GLib idle callback completes after one send.

    def session_requested(self, signaller, session, consumer, offer):
        with self.lock:
            entry = self.media_sessions.setdefault(consumer, {'created': time.monotonic()})
            entry['id'] = session
            rejected = entry.get('rejected', False)
        if rejected:
            GLib.idle_add(self.end_session, session)

    def consumer_added(self, _, consumer, rtc):
        rtc.set_property('latency', 0)
        address = self.args.host
        with self.lock:
            ports = range(self.args.rtc_port, self.args.rtc_port + self.args.max_media_sessions)
            port = next((item for item in ports if item not in self.media_ports.values()), None)
            entry = self.media_sessions.setdefault(consumer, {'created': time.monotonic(), 'id': None})
            denied = port is None or self.settings.snapshot()['applying']
            if denied:
                entry['rejected'] = True
            else:
                self.media_ports[consumer] = port
        if denied:
            # Do not gather unrestricted candidates while the queued close runs.
            rtc.set_property('ice-transport-policy', GstWebRTC.WebRTCICETransportPolicy.RELAY)
        ice, nice_agent = restrict_ice(rtc, address, port if port is not None else self.args.rtc_port)
        if denied:
            with self.lock:
                self.ice_agents[consumer] = (ice, nice_agent)
            if entry['id']:
                GLib.idle_add(self.end_session, entry['id'])
            return
        with self.lock:
            self.consumers[consumer] = rtc
            # restrict_ice repairs native ownership; retain these wrappers for
            # peer diagnostics until this consumer is removed.
            self.ice_agents[consumer] = (ice, nice_agent)
            self.cameras.set_active(True)
        print('Media consumer added:', consumer, flush=True)

    def consumer_removed(self, _, consumer, rtc):
        with self.lock:
            self.consumers.pop(consumer, None)
            self.encoders.pop(consumer, None)
            self.ice_agents.pop(consumer, None)
            self.media_ports.pop(consumer, None)
            self.media_sessions.pop(consumer, None)
            self.last_consumer_at = time.monotonic()
        print('Consumer removed:', consumer, flush=True)

    def encoder_setup(self, _, consumer, pad, encoder):
        factory = encoder.get_factory().get_name()
        if factory != self.args.encoder:
            self.source_error = f'Unexpected encoder {factory}; requested {self.args.encoder}'
            self.stopped.set()
            return False
        if factory == 'x264enc':
            Gst.util_set_object_arg(encoder, 'tune', 'zerolatency')
            Gst.util_set_object_arg(encoder, 'speed-preset', 'ultrafast')
            settings = {'bitrate': self.args.bitrate // 1000, 'key-int-max': max(1, round(self.args.fps * self.args.keyframe_interval)),
                        'bframes': 0, 'b-adapt': False, 'threads': 2, 'vbv-buf-capacity': 100,
                        'rc-lookahead': 0, 'sync-lookahead': 0, 'byte-stream': True}
            factor = 1000
        elif factory == 'nvv4l2h264enc':
            interval = max(1, round(self.args.fps * self.args.keyframe_interval))
            settings = {'bitrate': self.args.bitrate, 'iframeinterval': interval,
                        'idrinterval': interval, 'insert-sps-pps': True, 'num-B-Frames': 0,
                        'control-rate': 1, 'vbv-size': self.args.bitrate // 10}
            factor = 1
        else:
            self.source_error = f'Unexpected encoder {factory}; requested {self.args.encoder}'
            self.stopped.set()
            return False
        for key, value in settings.items():
            if encoder.find_property(key):
                encoder.set_property(key, value)
        if consumer != 'discovery':
            state = {'element': encoder, 'factory': factory, 'factor': factor,
                     'frames': 0, 'bytes': 0, 'keyframes': 0, 'keyframe_requests': 0,
                     'last_keyframe_ms': None}
            if self.full_diagnostics:
                encoder.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self.encoded, state)
                encoder.get_static_pad('src').add_probe(Gst.PadProbeType.EVENT_UPSTREAM, self.encoder_event, state)
            with self.lock:
                self.encoders[consumer] = state
        print('Encoder setup:', consumer, factory, flush=True)
        # After initialization, webrtcsink owns encoder bitrate allocation.
        return True

    def payloader_setup(self, _, consumer, pad, payloader):
        if payloader.find_property('config-interval'):
            payloader.set_property('config-interval', -1)
        return False

    def encoded(self, pad, info, state):
        buffer = info.get_buffer()
        state['frames'] += 1
        state['bytes'] += buffer.get_size()
        if not buffer.has_flags(Gst.BufferFlags.DELTA_UNIT):
            state['keyframes'] += 1
            state['last_keyframe_ms'] = time.monotonic() * 1000
        return Gst.PadProbeReturn.OK

    def encoder_event(self, pad, info, state):
        event = info.get_event()
        structure = event.get_structure() if event else None
        if structure and structure.get_name() == 'GstForceKeyUnit':
            state['keyframe_requests'] += 1
        return Gst.PadProbeReturn.OK

    def push(self, data, age_ms=0, placeholder=False):
        self.source_age_ms = age_ms
        if age_ms > self.args.max_source_age_ms:
            self.source_old_frames += 1
            return
        now = time.monotonic()
        if self.full_diagnostics and self.frames % self.args.fps == 0:
            luma = data[:self.args.width * (self.args.height - 32):32]
            self.source_luma = {'mean': float(luma.mean()), 'min': int(luma.min()), 'max': int(luma.max())}
        if self.args.save_source_preview and self.frames == self.args.fps:
            import cv2
            image = cv2.cvtColor(data.reshape(self.args.height * 3 // 2, self.args.width), cv2.COLOR_YUV2BGR_NV12)
            cv2.imwrite(str(self.output / 'source-preview.jpg'), image)
        if self.args.stamp and not placeholder:
            data = data.copy()
            stamp_nv12(data, self.args.width, self.args.height, now * 1000 - age_ms)
        packet = data.tobytes()
        buffer = Gst.Buffer.new_allocate(None, len(packet), None)
        buffer.fill(0, packet)
        buffer.pts = self.pipeline.get_clock().get_time() - self.pipeline.get_base_time()
        buffer.duration = Gst.SECOND // self.args.fps
        result = self.source.emit('push-buffer', buffer)
        if result not in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING):
            raise RuntimeError(f'Camera input failed: {result}')
        self.frames += 1

    def capture(self):
        import numpy as np
        blank = np.full(self.args.width * self.args.height * 3 // 2, 128, dtype=np.uint8)
        blank[:self.args.width * self.args.height] = 24
        seen, last_blank = -1, 0
        try:
            while not self.stopped.is_set() and not self.feed_stopped.is_set():
                with self.lock:
                    if not self.consumers and time.monotonic() - self.last_consumer_at >= self.args.idle_release_s:
                        self.cameras.set_active(False)
                frame = self.cameras.wait_for_frame(seen, self.args.health_check_interval_ms / 1000)
                now = time.monotonic()
                if frame and frame[0] != seen:
                    seen = frame[0]
                    self.push(frame[1], max(0, (now - frame[2]) * 1000))
                elif (not frame or (now - frame[2]) * 1000 > self.args.max_source_age_ms) and (now - last_blank) * 1000 >= self.args.placeholder_interval_ms:
                    # Keep negotiation/data alive without displaying an old view as live.
                    self.push(blank, placeholder=True)
                    last_blank = now
        except Exception as error:
            self.source_error = repr(error)
            print('Capture failed:', self.source_error, flush=True)
            self.feed_stopped.set()

    def record_settings(self, event):
        with (self.output / 'settings.jsonl').open('a') as log:
            log.write(json.dumps(event) + '\n')

    def set_profile(self, name, policy):
        """Select a profile only after admission has excluded existing viewers."""
        fields = (*settings_from_args(self.args), 'stamp', 'max_source_age_ms',
                  'video_max_age_ms', 'video_stall_ms')
        target = {key: policy[key] for key in fields} | {'profile': name}
        with self.lock:
            old = self.settings
            snapshot = old.snapshot()
            if snapshot['applying'] or self.consumers:
                raise SettingsConflict('Disconnect video viewers before changing profile')
            previous = {key: getattr(self.args, key) for key in target}
            with old.lock:
                old.applying = True
        error = None
        try:
            self.apply_settings(target)
        except Exception as cause:
            error = str(cause)
            try:
                self.apply_settings(previous)
            except Exception as rollback:
                with old.lock:
                    old.failed, old.error = True, f'{cause}; rollback failed: {rollback}'
            raise
        finally:
            with self.lock:
                with old.lock:
                    old.applying = False
                if error is None:
                    old.close()
                    self.settings = StreamSettings(settings_from_args(self.args), self.apply_settings, self.record_settings)
                    self.settings.revision = snapshot['revision'] + 1
                    self.settings.change_id = snapshot['change_id'] + 1
            self.record_settings({'profile': name, 'error': error, 'mono_ms': time.monotonic() * 1000})

    def request_settings(self, value):
        with self.lock:
            if self.consumers:
                raise SettingsConflict('Disconnect video viewers before changing shared stream settings')
            return self.settings.request(value)

    def select_camera(self, identity):
        with self.lock:
            if self.settings.snapshot()['applying']:
                raise SettingsConflict('Wait for the stream settings change before switching cameras')
            self.cameras.select(identity)

    def apply_settings(self, values):
        """Rebuild the sender while retaining HTTP and camera discovery.

        The pinned 0.14.5 sink does not reset initial_discovery_started on
        unprepare, so reusing it across READY/PLAYING cannot restart signalling.
        Video consumers reconnect. Independent control connections stay open.
        """
        if self.stopped.is_set():
            raise RuntimeError('Server is stopping')
        was_streaming = self.cameras.snapshot()['state'] == 'streaming'
        self.feed_stopped.set()
        if hasattr(self, 'capture_thread'):
            self.capture_thread.join(timeout=3)
            if self.capture_thread.is_alive():
                raise RuntimeError('Previous frame sender did not stop')
        if self.pipeline.set_state(Gst.State.NULL) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('Could not stop the previous sender')
        result, state, pending = self.pipeline.get_state(5 * Gst.SECOND)
        if result == Gst.StateChangeReturn.FAILURE or state != Gst.State.NULL:
            raise RuntimeError('Previous sender did not stop')
        for element, handler in self.pipeline_handlers:
            element.disconnect(handler)
        self.pipeline_handlers.clear()
        with self.lock:
            self.consumers.clear()
            self.encoders.clear()
            self.ice_agents.clear()
            self.media_ports.clear()
            self.media_sessions.clear()
        self.args = argparse.Namespace(**(vars(self.args) | values))
        changed = self.cameras.configure(values['width'], values['height'], values['fps'], policy=vars(self.args))
        if changed and was_streaming:
            deadline = time.monotonic() + 20
            while self.cameras.frame() is None:
                camera = self.cameras.snapshot()
                if camera['state'] == 'unavailable':
                    raise RuntimeError(camera['error'] or 'Camera could not open this output mode')
                if self.stopped.wait(.05):
                    raise RuntimeError('Server is stopping')
                if time.monotonic() > deadline:
                    raise RuntimeError('Camera did not produce a frame in the requested output mode')
        if self.stopped.is_set():
            raise RuntimeError('Server is stopping')
        with self.lock:
            self.build_pipeline()
        self.source_error = None
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('Reconfigured WebRTC pipeline could not start')
        self.feed_stopped.clear()
        self.capture_thread = threading.Thread(target=self.capture, daemon=True)
        self.capture_thread.start()
        # Surface asynchronous negotiation errors while rollback is still possible.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if message:
                error, debug = message.parse_error()
                raise RuntimeError(str(error))
            if self.source_error:
                raise RuntimeError(self.source_error)
            if self.stopped.wait(.05):
                raise RuntimeError('Server is stopping')

    def snapshot(self):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # GStreamer may call into Python while holding its own object lock.
        # Pin references here, then query properties without our callback lock.
        with self.lock:
            encoder_states = {key: value.copy() for key, value in self.encoders.items()}
            consumer_refs = self.consumers.copy()
            ice_refs = self.ice_agents.copy()
            media_ports = self.media_ports.copy()
            source, queue, sink = self.source, self.queue, self.sink
        encoders = {}
        for name, state in encoder_states.items():
            element = state['element']
            caps = element.get_static_pad('sink').get_current_caps()
            encoders[name] = {key: value for key, value in state.items() if key not in ('element', 'factor')}
            encoders[name].update(bitrate_bps=element.get_property('bitrate') * state['factor'],
                                  input_caps=caps.to_string() if caps else None)
        consumers = {key: rtc.get_property('connection-state').value_nick for key, rtc in consumer_refs.items()}
        ice_upnp = {key: agent.get_property('upnp') for key, (_, agent) in ice_refs.items()}
        return {'mono_ms': time.monotonic() * 1000, 'phase': self.phase, 'measuring': self.measuring,
                'process_cpu_seconds': usage.ru_utime + usage.ru_stime, 'peak_rss_mib': usage.ru_maxrss / 1024,
                'mode': self.args.mode, 'source': self.args.source, 'source_frames': self.frames,
                'settings': self.settings.snapshot() | {'effective': settings_from_args(self.args),
                                                        'controls_paused': True},
                'source_age_ms': self.source_age_ms, 'source_old_frames': self.source_old_frames,
                'source_luma': self.source_luma,
                'error': self.source_error, 'appsrc_buffers': source.get_property('current-level-buffers'),
                'raw_queue_buffers': queue.get_property('current-level-buffers'),
                'encoders': encoders, 'consumers': consumers,
                'ice_upnp': ice_upnp,
                'media_ports': media_ports,
                'camera': self.cameras.snapshot(),
                'actuators_enabled': False,
                'webrtc': structure_json(sink.get_property('stats')) if self.full_diagnostics else None,
                'diagnostics': {'level': self.args.diagnostics,
                    'dropped_records': sum(log.dropped for log in (self.log, self.browser_log)),
                    'log_errors': [log.error for log in (self.log, self.browser_log) if log.error]},
                'browser_age_ms': (time.monotonic() - self.last_browser_at) * 1000 if self.last_browser_at else None}

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('WebRTC pipeline could not start')
        self.cameras.start()
        self.capture_thread = threading.Thread(target=self.capture, daemon=True)
        self.capture_thread.start()

    def check(self):
        now = time.monotonic()
        with self.lock:
            sessions = self.media_sessions.copy()
            consumers = self.consumers.copy()
        for consumer, session in sessions.items():
            if now - session['created'] <= self.args.media_connect_timeout_s:
                continue
            rtc = consumers.get(consumer)
            state = rtc.get_property('connection-state').value_nick if rtc else 'new'
            if state in ('new', 'connecting', 'failed', 'closed', 'disconnected'):
                if session.get('id'):
                    self.end_session(session['id'])
                if rtc is None:
                    with self.lock:
                        self.media_sessions.pop(consumer, None)
                        self.media_ports.pop(consumer, None)

    def close(self):
        self.stopped.set()
        self.feed_stopped.set()
        self.settings.close()
        try:
            self.cameras.close()
        finally:
            if hasattr(self, 'capture_thread'):
                self.capture_thread.join(timeout=8)
            self.pipeline.set_state(Gst.State.NULL)
            self.ice_agents.clear()
            self.log.close()
            self.browser_log.close()

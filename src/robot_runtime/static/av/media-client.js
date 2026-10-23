import {observable} from './core.js';
import {stampAge, videoFreshness} from './freshness.js';

// The pinned upstream facade lacks a public close method. Keep that one private
// compatibility detail here, disable its unowned retry timer, and own retries.
async function createSignalling(options) {
  const {default: GstWebRTCAPI} = await import('./vendor/gstwebrtc-api-3.0.0.esm.js');
  const api = new GstWebRTCAPI({...options, reconnectionTimeout: 0});
  return {api, close() {
    api.unregisterAllConnectionListeners(); api.unregisterAllPeerListeners();
    const channel = api._channel; api._channel = null;
    channel?.close();
  }};
}

export class MediaClient {
  constructor({api, profile = 'observer', signallingUrl, signallingFactory = createSignalling,
    now = () => performance.now(), document = globalThis.document} = {}) {
    Object.assign(this, {api, profile, signallingUrl, signallingFactory, now, document});
    this.events = observable({status: 'disconnected', error: '', profile, report: null,
      freshness: {fresh: false, age: null, arrivalAge: null, reason: 'No video connection'}, clock: null});
    this.users = 0; this.generation = 0; this.transportGeneration = 0; this.video = null; this.closed = false;
    this.want = false; this.connecting = false; this.session = null; this.signalling = null; this.producer = null;
    this.token = null; this.renewAt = 0; this.config = null; this.clock = null; this.lastFrame = null;
    this.content = null; this.displayed = 0; this.samples = []; this.reports = []; this.previous = null;
    this.stampAttempts = this.stampValid = this.uploadErrors = 0; this.frameCallback = null;
    this.sessionHandlers = []; this.intervals = []; this.settingsChange = null;
  }
  get state() { return this.events.state; }
  subscribe(fn) { return this.events.subscribe(fn); }
  publish(value) { this.events.publish({...this.state, ...value}); }
  freshness() {
    if (!this.config || !this.session) return {fresh: false, age: null, arrivalAge: null, reason: 'No video connection'};
    const state = videoFreshness(this.now(), this.lastFrame, this.content, this.clock, this.config);
    const camera = this.api.state.host?.camera;
    if (camera && (camera.state !== 'streaming' || camera.frame_age_ms === null || camera.frame_age_ms > this.config.max_source_age_ms)) {
      return {...state, fresh: false, reason: 'Camera unavailable · waiting for selected device'};
    }
    // Arrival can describe observer liveness, but cannot establish FPV content age.
    return {...state, arriving: state.arrivalAge !== null && state.arrivalAge < this.config.video_stall_ms};
  }
  acquire() {
    if (this.closed) throw Error('Media client has been disposed');
    if (++this.users === 1) {
      this.releaseApi = this.api.acquire();
      this.unsubscribeApi = this.api.subscribe(state => this.backend(state));
      this.intervals.push(setInterval(() => this.publish({freshness: this.freshness()}), 100));
      this.intervals.push(setInterval(() => this.tick(), 1000));
    }
    let released = false;
    return () => { if (!released) { released = true; if (--this.users === 0) this.stop(); } };
  }
  attachVideo(video) {
    if (this.video && this.video !== video) throw Error('A MediaClient has one video attachment; share its state with other views');
    this.video = video;
    return () => { if (this.video === video) { this.disconnect(); this.video = null; } };
  }
  backend({config, host, online}) {
    if (!config) return;
    if (this.want && this.config?.runtime && JSON.stringify(this.config.runtime) !== JSON.stringify(config.runtime)) {
      this.disconnect(); this.publish({error: 'Runtime ownership changed. Connect explicitly again.'});
    }
    this.config = config;
    const settings = host?.settings || config.settings;
    if (this.settingsChange === null) this.settingsChange = settings.change_id;
    if (settings.applying && this.session) {
      this.closeTransport(); this.connectStarted = this.now(); this.publish({status: 'reconnecting', error: ''});
    }
    if (!settings.applying && this.settingsChange !== settings.change_id) {
      this.settingsChange = settings.change_id;
      this.connectStarted = this.now();
      this.closeTransport();
      if (this.want) this.openSignalling();
    }
    if (!online && this.want) this.publish({error: 'Jetson unavailable · check the server and connection'});
  }
  async connect(profile = this.profile) {
    if (this.closed) throw Error('Media client has been disposed');
    if (this.want || this.connecting) return;
    if (!this.video) throw Error('Attach a video view before connecting');
    this.want = true; this.connecting = true;
    this.connectStarted = this.now();
    const generation = ++this.generation;
    this.publish({status: 'connecting', error: ''});
    try {
      const grant = await this.api.request('media/sessions', {profile}, {timeout: 35000});
      if (!this.want || generation !== this.generation) {
        this.api.request('media/close', {token: grant.token}).catch(() => {}); return;
      }
      this.token = grant.token; this.renewAt = this.now() + grant.ttl_s * 1000 / 3;
      this.renewEvery = grant.ttl_s * 1000 / 3;
      this.profile = profile;
      this.config = await this.api.request('config');
      this.settingsChange = this.config.settings.change_id;
      this.publish({profile: grant.profile});
      await this.openSignalling();
      this.calibrate();
    } catch (error) {
      if (generation === this.generation) { this.disconnect(); this.publish({status: 'error', error: error.message}); }
    } finally { this.connecting = false; }
  }
  async openSignalling() {
    if (!this.want || !this.token || this.signalling || this.opening) return;
    this.opening = true;
    const generation = this.generation;
    const transportGeneration = this.transportGeneration;
    try {
      const config = this.config = await this.api.request('config');
      if (!this.want || generation !== this.generation || transportGeneration !== this.transportGeneration) return;
      const rtc = {iceServers: config.ice_servers, iceTransportPolicy: config.turn_port ? 'relay' : 'all', bundlePolicy: 'max-bundle'};
      const address = this.signallingUrl || this.api.signalUrl(this.token);
      const signalling = await this.signallingFactory({meta: {name: 'Robot runtime media viewer'}, signalingServerUrl: address, webrtcConfig: rtc});
      if (!this.want || generation !== this.generation || transportGeneration !== this.transportGeneration) { signalling.close(); return; }
      this.signalling = signalling;
      signalling.api.registerConnectionListener({
        connected: () => this.publish({status: 'waiting', error: ''}),
        disconnected: () => {
          if (this.signalling === signalling) { this.closeTransport(); this.publish({status: 'reconnecting'}); }
        },
      });
      signalling.api.registerPeerListener({
        producerAdded: value => { if (this.signalling === signalling) { this.producer = value; this.startSession(); } },
        producerRemoved: value => { if (this.producer?.id === value.id) this.producer = null; },
      });
      this.producer = signalling.api.getAvailableProducers()[0] || null;
      if (this.producer) this.startSession();
    } catch (error) { if (generation === this.generation) this.publish({status: 'reconnecting', error: error.message}); }
    finally { this.opening = false; }
  }
  startSession() {
    if (!this.want || this.session || !this.producer || this.config.settings.applying) return;
    const current = this.signalling.api.createConsumerSession(this.producer.id);
    if (!current) { this.publish({error: 'Signalling is not ready'}); return; }
    this.session = current; this.previous = null; this.lastFrame = this.content = null;
    this.displayed = this.stampAttempts = this.stampValid = 0; this.samples = []; this.sentSdp = false;
    const on = (type, fn) => { current.addEventListener(type, fn); this.sessionHandlers.push(() => current.removeEventListener(type, fn)); };
    on('streamsChanged', () => {
      if (this.session !== current || !this.video) return;
      this.video.srcObject = current.streams[0] || null;
      this.video.play().catch(error => { if (this.session === current) this.publish({error: error.message}); });
      for (const receiver of current.rtcPeerConnection?.getReceivers() || []) {
        if ('jitterBufferTarget' in receiver) receiver.jitterBufferTarget = 0;
      }
      this.publish({status: 'connecting', error: ''});
    });
    on('error', event => this.publish({error: `${event.message}: ${event.error?.message || ''}`}));
    on('closed', () => { if (this.session === current) { this.closeTransport(); this.publish({status: 'reconnecting'}); } });
    if (!current.connect()) { this.closeTransport(); this.publish({error: 'Could not start the video session'}); return; }
    if (this.video.requestVideoFrameCallback) this.frameCallback = this.video.requestVideoFrameCallback((now, metadata) => this.frame(now, metadata, current));
    else this.publish({error: 'This browser cannot report video frame freshness; use current Chrome or Edge'});
  }
  frame(now, metadata, session) {
    if (this.session !== session || !this.video) return;
    this.lastFrame = now; this.content = null; this.displayed++;
    if (this.state.status !== 'connected') this.publish({status: 'connected', error: ''});
    const config = this.config, clock = this.clock;
    if (config.stamp && clock && this.now() - clock.at < config.clock_valid_ms && metadata.width >= 160) {
      if (!this.canvas) { this.canvas = this.document.createElement('canvas'); this.context = this.canvas.getContext('2d', {willReadFrequently: true}); }
      const canvas = this.canvas, ctx = this.context;
      canvas.width = 640; canvas.height = 32;
      const stripHeight = metadata.height * 32 / config.height;
      ctx.drawImage(this.video, 0, metadata.height - stripHeight, metadata.width, stripHeight, 0, 0, 640, 32);
      const pixels = ctx.getImageData(0, 0, 640, 32).data, bytes = [];
      for (let byte = 0; byte < 6; byte++) {
        let value = 0;
        for (let bit = 0; bit < 8; bit++) {
          const x = Math.floor(8 + (byte * 8 + bit + .5) * 13), offset = (16 * 640 + x) * 4;
          value = value * 2 + (pixels[offset] + pixels[offset + 1] + pixels[offset + 2] > 384 ? 1 : 0);
        }
        bytes.push(value);
      }
      this.stampAttempts++;
      if (bytes[0] === 0xa5 && bytes.reduce((a, b) => a ^ b, 0) === 0) {
        const stamp = ((bytes[1] * 256 + bytes[2]) * 256 + bytes[3]) * 256 + bytes[4];
        const age = stampAge(now, clock.offset, stamp), delay = stampAge(metadata.expectedDisplayTime, clock.offset, stamp);
        if (age !== null) this.content = {age, at: now, uncertainty: clock.uncertainty};
        if (delay !== null) { this.samples.push(delay); this.stampValid++; }
      }
    }
    this.frameCallback = this.video.requestVideoFrameCallback((next, details) => this.frame(next, details, session));
  }
  async calibrate() {
    if (this.syncing || !this.want || !this.config?.stamp) return;
    this.syncing = true;
    const generation = this.generation;
    try {
      const probes = [];
      for (let index = 0; index < this.config.clock_probe_count; index++) {
        if (generation !== this.generation) return;
        const sent = this.now(), data = await this.api.request('clock', undefined, {timeout: 2000}), received = this.now();
        probes.push({rtt: received - sent, offset: data.mono_ms - (sent + received) / 2});
      }
      probes.sort((a, b) => a.rtt - b.rtt);
      if (generation === this.generation) { this.clock = {offset: probes[0].offset, uncertainty: probes[0].rtt / 2, at: this.now()}; this.publish({clock: this.clock}); }
    } catch (error) { if (generation === this.generation) this.publish({error: `Clock calibration: ${error.message}`}); }
    finally { this.syncing = false; }
  }
  async tick() {
    if (!this.want || this.ticking) return;
    this.ticking = true;
    try {
      if (this.token && this.now() >= this.renewAt) {
        try { await this.api.request('media/renew', {token: this.token}); this.renewAt = this.now() + this.renewEvery; }
        catch (error) { this.disconnect(); this.publish({error: `Media subscription lost: ${error.message}. Reconnect to retry.`}); return; }
      }
      if (!this.clock || this.now() - this.clock.at >= this.config.clock_sync_interval_ms) this.calibrate();
      if (!this.api.state.host?.settings?.applying && !this.signalling) await this.openSignalling();
      if (this.producer && !this.session) this.startSession();
      if (!this.api.state.host?.settings?.applying && this.lastFrame === null &&
          this.now() - this.connectStarted > (this.config.media_connect_timeout_s || 30) * 1000) {
        this.disconnect(); this.publish({error: 'Video did not connect in time. Check the media path, then reconnect.'}); return;
      }
      const pc = this.session?.rtcPeerConnection;
      if (pc) await this.telemetry(pc);
    } catch (error) { this.publish({error: error.message}); }
    finally { this.ticking = false; }
  }
  async telemetry(pc) {
    const stats = await pc.getStats();
    if (pc !== this.session?.rtcPeerConnection) return;
    const host = this.api.state.host, config = this.config, now = this.now();
    const report = {client_now_ms: now, sample_server_mono_ms: this.clock ? now + this.clock.offset : host?.mono_ms,
      sample_time_basis: this.clock ? 'calibrated-client' : 'host-stats', clock_uncertainty_ms: this.clock?.uncertainty ?? null,
      phase_observed: host?.phase, visibility: this.document.visibilityState, connection_state: pc.connectionState,
      displayed_frames: this.displayed, last_frame_age_ms: this.lastFrame === null ? null : now - this.lastFrame,
      content_age_upper_bound_ms: this.freshness().age, latency_ms: this.samples.splice(0),
      stamp_attempts: this.stampAttempts, stamp_valid: this.stampValid, upload_errors: this.uploadErrors,
      settings_revision: host?.settings?.revision};
    for (const row of stats.values()) {
      if (row.type === 'transport' && row.selectedCandidatePairId) {
        const pair = stats.get(row.selectedCandidatePairId);
        report.ice_rtt_ms = pair?.currentRoundTripTime * 1000;
        report.local_candidate = stats.get(pair?.localCandidateId); report.remote_candidate = stats.get(pair?.remoteCandidateId);
        report.selected_pair = {state: pair?.state, nominated: pair?.nominated, bytes_sent: pair?.bytesSent, bytes_received: pair?.bytesReceived};
      }
      if (row.type !== 'inbound-rtp' || row.kind !== 'video') continue;
      const previous = this.previous;
      const seconds = previous ? (row.timestamp - previous.timestamp) / 1000 : 0;
      const emitted = previous ? row.jitterBufferEmittedCount - previous.jitterBufferEmittedCount : 0;
      Object.assign(report, {frames_decoded: row.framesDecoded, frames_dropped: row.framesDropped,
        fps: seconds ? (row.framesDecoded - previous.framesDecoded) / seconds : null,
        mbps: seconds ? (row.bytesReceived - previous.bytesReceived) * 8 / seconds / 1e6 : null,
        width: row.frameWidth, height: row.frameHeight, packets_received: row.packetsReceived, packets_lost: row.packetsLost,
        pli_count: row.pliCount, nack_count: row.nackCount, keyframes_decoded: row.keyFramesDecoded,
        retransmitted_packets: row.retransmittedPacketsReceived, freeze_count: row.freezeCount, total_freezes_s: row.totalFreezesDuration,
        jitter_buffer_ms: emitted ? (row.jitterBufferDelay - previous.jitterBufferDelay) * 1000 / emitted : null, decoder: row.decoderImplementation});
      this.previous = row;
    }
    const candidates = [report.local_candidate, report.remote_candidate];
    const turn = candidates.some(c => c?.candidateType === 'relay' || c?.relayProtocol || c?.url?.startsWith('turn:') || c?.url?.startsWith('turns:'));
    report.transport_path = turn ? (config.turn_tcp ? 'turn_tcp' : 'turn_udp') :
      !config.turn_port && report.selected_pair?.state === 'succeeded' && report.frames_decoded > 0 && candidates.every(c => c?.protocol === 'udp') ?
        (config.relay ? 'udp_test_shaper' : 'direct_udp') : 'unknown';
    if (config.diagnostics === 'full' && !this.sentSdp && pc.remoteDescription && pc.localDescription) {
      report.remote_sdp = pc.remoteDescription.sdp; report.local_sdp = pc.localDescription.sdp;
      report.ice_servers = pc.getConfiguration().iceServers.map(({urls}) => ({urls})); this.sentSdp = true;
    }
    this.reports.push(report); if (this.reports.length > 7200) this.reports.shift();
    this.publish({report, freshness: this.freshness()});
    if (!this.posting) {
      this.posting = true;
      this.api.request('telemetry', report).catch(() => this.uploadErrors++).finally(() => { this.posting = false; });
    }
  }
  closeTransport() {
    // A long-running stream gets a new connection window when it closes. The
    // peer can close before the next HTTP poll reports a settings rebuild.
    if (this.lastFrame !== null) this.connectStarted = this.now();
    this.transportGeneration++;
    this.producer = null;
    for (const remove of this.sessionHandlers.splice(0)) remove();
    const session = this.session; this.session = null; session?.close();
    this.signalling?.close(); this.signalling = null;
    if (this.frameCallback !== null) this.video?.cancelVideoFrameCallback?.(this.frameCallback);
    this.frameCallback = null;
    if (this.video) this.video.srcObject = null;
    this.lastFrame = this.content = null;
    this.publish({freshness: this.freshness(), report: null});
  }
  disconnect() {
    this.want = false; ++this.generation;
    this.closeTransport();
    const token = this.token; this.token = null;
    if (token) this.api.request('media/close', {token}, {keepalive: true}).catch(() => {});
    this.publish({status: 'disconnected', error: ''});
  }
  exportData() {
    const config = this.config ? {...this.config, ice_servers: this.config.ice_servers.map(({urls}) => ({urls}))} : null;
    return {config, reports: this.reports, last_host: this.api.state.host};
  }
  stop() {
    this.disconnect();
    this.unsubscribeApi?.(); this.releaseApi?.(); this.unsubscribeApi = this.releaseApi = null;
    this.intervals.splice(0).forEach(clearInterval);
  }
  dispose() { this.stop(); this.closed = true; this.events.clear(); this.canvas = this.context = null; }
}

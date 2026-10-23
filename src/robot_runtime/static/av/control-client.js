import {neutralInput, observable} from './core.js';

function gather(peer, timeout) {
  if (peer.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve, reject) => {
    const done = error => { clearTimeout(timer); peer.removeEventListener('icegatheringstatechange', change);
      error ? reject(error) : resolve(); };
    const change = () => { if (peer.iceGatheringState === 'complete') done(); };
    const timer = setTimeout(() => done(Error('Control ICE gathering timed out')), timeout);
    peer.addEventListener('icegatheringstatechange', change);
  });
}

// Owns one data-only peer. It never opens a camera or creates a video element.
export class ControlClient {
  constructor({input, api, mode = 'input', videoState = () => ({fresh: false, age: null, reason: 'No live image'}),
    peerFactory = config => new RTCPeerConnection(config), now = () => performance.now(),
    document = globalThis.document, window = globalThis.window,
    interval = (fn, ms) => setInterval(fn, ms), cancelInterval = id => clearInterval(id)} = {}) {
    Object.assign(this, {input, api, mode, videoState, peerFactory, now, document, window, interval, cancelInterval});
    if (!['input', 'fpv'].includes(mode)) throw Error('Unknown control mode');
    this.events = observable({status: 'disconnected', enabled: false, canEnable: false, reason: 'Connect input first',
      ack: null, receiptFresh: false, rtt: null, ackAge: null, sent: 0, skipped: 0, sessionId: null});
    this.enabled = false; this.enabledAt = 0; this.seq = 0; this.sent = 0; this.skipped = 0;
    this.generation = 0; this.peer = null; this.channel = null; this.timer = null; this.removers = []; this.users = 0;
    this.lease = null; this.ack = null; this.ackAt = null; this.rtt = null; this.closed = false;
  }
  get state() { return this.events.state; }
  subscribe(fn) { return this.events.subscribe(fn); }
  acquire() {
    if (this.closed) throw Error('Control client has been disposed');
    if (++this.users === 1) {
      this.releaseApi = this.api.acquire();
      this.unsubscribeApi = this.api.subscribe(({config}) => {
        if (this.peer && this.runtimeIdentity && config?.runtime &&
            JSON.stringify(this.runtimeIdentity) !== JSON.stringify(config.runtime)) {
          this.disconnect('Runtime ownership changed. Connect explicitly again.');
        }
      });
    }
    let released = false;
    return () => { if (!released) { released = true; if (--this.users === 0) {
      this.disconnect(); this.releaseApi?.(); this.unsubscribeApi?.();
    } } };
  }
  focused() { return !this.document || (!this.document.hidden && this.document.hasFocus()); }
  video() { return this.mode === 'fpv' ? this.videoState() : {fresh: true, age: null, reason: 'Input-only session'}; }
  listen(target, type, fn) { target?.addEventListener(type, fn); this.removers.push(() => target?.removeEventListener(type, fn)); }
  publish(extra = {}) {
    if (extra.reason !== undefined) this.pauseReason = extra.reason;
    const video = this.video(), at = this.now();
    const age = this.ackAt === null ? null : at - this.ackAt;
    const recent = age !== null && this.rtt !== null && age + this.rtt < this.policy.input_max_age_ms;
    const canEnable = this.channel?.readyState === 'open' && Boolean(this.lease) && video.fresh && this.focused();
    this.events.publish({...this.state, enabled: this.enabled, canEnable, ack: this.ack,
      receiptFresh: Boolean(recent && this.ack?.active), rtt: this.rtt, ackAge: age,
      sent: this.sent, skipped: this.skipped,
      reason: this.enabled ? 'Enabled · diagnostic input only' : !this.focused() ? 'Focus this page to resume' :
        !video.fresh ? video.reason : this.pauseReason || 'Paused · enable to send input', ...extra});
  }
  async connect() {
    if (this.closed) throw Error('Control client has been disposed');
    if (this.peer || this.state.status === 'connecting') return;
    const generation = ++this.generation;
    this.events.publish({...this.state, status: 'connecting', reason: 'Connecting diagnostic input', error: ''});
    try {
      const config = await this.api.request('config');
      if (generation !== this.generation) return;
      this.policy = config.control_policy || config;
      this.runtimeIdentity = config.runtime;
      this.connectStarted = this.now(); this.connectTimeout = config.control_negotiate_timeout_s * 3000;
      const peer = this.peer = this.peerFactory({iceServers: config.ice_servers,
        iceTransportPolicy: config.ice_servers?.length ? 'relay' : 'all', bundlePolicy: 'max-bundle'});
      this.releaseInput = this.input.acquire();
      const channel = this.channel = peer.createDataChannel('robot-gamepad-v2', {ordered: false, maxRetransmits: 0});
      this.listen(channel, 'open', () => this.publish({status: 'connected', reason: 'Paused · enable to send input'}));
      this.listen(channel, 'message', event => this.receive(event.data));
      this.listen(channel, 'close', () => this.disconnect('Control connection closed'));
      this.listen(peer, 'connectionstatechange', () => {
        if (['failed', 'closed', 'disconnected'].includes(peer.connectionState)) this.disconnect('Control connection lost');
      });
      for (const type of ['blur', 'pagehide']) this.listen(this.window, type, () => this.pause('Paused · page lost focus'));
      this.listen(this.document, 'visibilitychange', () => { if (this.document.hidden) this.pause('Paused · page hidden'); });
      this.timer = this.interval(() => this.pulse(), 1000 / this.policy.input_rate_hz);
      await peer.setLocalDescription(await peer.createOffer());
      await gather(peer, config.control_negotiate_timeout_s * 1000);
      if (generation !== this.generation) return;
      const answer = await this.api.request('control/sessions', {mode: this.mode, sdp: peer.localDescription.sdp},
        {timeout: config.control_negotiate_timeout_s * 4000 + 2000});
      if (generation !== this.generation) {
        await this.api.request('control/close', {session_id: answer.session_id}).catch(() => {});
        return;
      }
      this.sessionId = answer.session_id;
      this.publish({sessionId: this.sessionId});
      await peer.setRemoteDescription({type: 'answer', sdp: answer.sdp});
    } catch (error) {
      if (generation === this.generation) { this.disconnect(error.message); this.events.publish({...this.state, error: error.message}); }
    }
  }
  receive(text) {
    if (typeof text !== 'string' || text.length > 8192) return;
    try {
      const value = JSON.parse(text);
      if (!['gamepad-lease', 'gamepad-ack'].includes(value.type) || typeof value.epoch !== 'string' ||
          value.epoch.length !== 32 || !Number.isSafeInteger(value.lease) || value.lease < 1 ||
          (this.lease && value.epoch !== this.lease.epoch)) return;
      if (!this.lease || value.lease > this.lease.lease) {
        this.lease = {epoch: value.epoch, lease: value.lease}; this.leaseAt = this.now();
      }
      if (value.type === 'gamepad-ack') {
        if (!Number.isSafeInteger(value.seq) || value.seq > this.seq || value.seq < 1 ||
            (this.ack && value.seq <= this.ack.seq) || !Number.isFinite(value.sent_ms) ||
            value.sent_ms > this.now() || value.sent_ms < 0 || typeof value.active !== 'boolean') return;
        this.ack = value; this.ackAt = this.now(); this.rtt = this.ackAt - value.sent_ms;
        if (this.enabled && value.seq >= this.enabledSeq && !value.enabled) this.pause('Paused by input receiver');
      }
      this.publish();
    } catch { /* Invalid messages cannot update receipt state. */ }
  }
  enable() {
    this.publish();
    if (!this.state.canEnable) return false;
    this.enabled = true; this.enabledAt = this.now(); this.enabledSeq = this.seq + 1; this.publish();
    return true;
  }
  pause(reason = 'Paused · enable to resume input') {
    this.enabled = false;
    this.transmit(neutralInput());
    this.publish({reason});
  }
  transmit(input) {
    if (this.channel?.readyState !== 'open') return;
    if (this.channel.bufferedAmount > 0) { this.skipped++; return; }
    if (!this.lease || (this.enabled && this.now() - this.leaseAt >= this.policy.input_max_age_ms)) {
      try { this.channel.send(JSON.stringify({type: 'lease-request'})); } catch { this.skipped++; }
      return;
    }
    const video = this.video();
    const packet = {type: 'gamepad', v: 2, seq: ++this.seq, sent_ms: Math.round(this.now() * 1000) / 1000,
      active: this.enabled && video.fresh && this.focused() && input.active, enabled: this.enabled,
      source: input.source, mapping: input.mapping, video_age_ms: video.age, ...this.lease,
      axes: input.axes.map(value => Math.round(value * 1000) / 1000),
      buttons: input.buttons.map(value => Math.round(value * 1000) / 1000)};
    try { this.channel.send(JSON.stringify(packet)); this.sent++; } catch { this.skipped++; }
  }
  pulse() {
    if (this.state.status === 'connecting' && this.now() - this.connectStarted > this.connectTimeout) {
      this.disconnect('Control connection timed out · reconnect to retry'); return;
    }
    if (this.enabled) {
      const acknowledgementAge = this.now() - (this.ackAt ?? this.enabledAt) + (this.rtt || 0);
      const reason = !this.focused() ? 'Paused · page lost focus' :
        !this.video().fresh ? 'Paused · video became stale' :
        this.channel?.readyState !== 'open' ? 'Paused · input connection closed' :
        acknowledgementAge >= this.policy.input_timeout_ms ?
          `Paused · input acknowledgement overdue (${Math.round(acknowledgementAge)} ms)` : null;
      if (reason) this.pause(reason);
    }
    // Animation frames can lag while timers still run. Read the device for this
    // packet, after receipt of its lease; never relabel an old rendered sample.
    this.transmit(this.input.sample ? this.input.sample() : this.input.state);
    this.publish();
  }
  disconnect(reason = 'Disconnected · connect to start input') {
    ++this.generation;
    if (this.policy) this.pause(reason);
    for (const remove of this.removers.splice(0)) remove();
    this.cancelInterval(this.timer); this.timer = null;
    this.channel?.close(); this.peer?.close(); this.channel = this.peer = null;
    this.releaseInput?.(); this.releaseInput = null;
    const id = this.sessionId; this.sessionId = null;
    if (id) this.api.request('control/close', {session_id: id}, {keepalive: true}).catch(() => {});
    this.lease = this.ack = this.rtt = this.ackAt = null;
    this.events.publish({...this.state, status: 'disconnected', enabled: false, canEnable: false,
      ack: null, rtt: null, ackAge: null, receiptFresh: false, reason, sessionId: null});
  }
  dispose() { this.disconnect(); this.closed = true; this.events.clear(); }
}

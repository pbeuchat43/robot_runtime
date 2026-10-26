// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul N. Beuchat
import {observable} from './core.js';

// Adapts reusable AV clients to the shared runtime HTTP/authentication contract.
// Construction is inert, mutations are explicit, and requests are never replayed.
export class RuntimeAVApi {
  constructor({http, capability, document = globalThis.document} = {}) {
    if (!/^[a-z][a-z0-9_-]{0,31}$/.test(capability)) throw Error('Invalid AV capability');
    Object.assign(this, {http, capability, document});
    this.events = observable({online: false, config: null, host: null, error: ''});
    this.path = `api/av/${capability}/`; this.users = 0; this.closed = false;
  }
  get state() { return this.events.state; }
  subscribe(fn) { return this.events.subscribe(fn); }
  async request(route, body, {timeout = 15000} = {}) {
    if (this.closed) throw Error('AV client has been disposed');
    if (body === undefined) {
      const result = await this.http.get(this.path + route, {timeoutMs: timeout});
      if (route === 'config') result.received_at = performance.now();
      return result;
    }
    const config = this.state.config || await this.request('config');
    const identity = config.runtime;
    return this.http.post(this.path + route, {request_id: crypto.randomUUID(),
      expires_at_ms: Math.floor(config.server_time_ms + performance.now() - config.received_at) + Math.min(timeout, 55000), config_revision: identity.config_revision,
      generation: identity.generation, expected_run: identity.run_id, source_run: identity.source_run,
      arguments: body}, {timeoutMs: timeout});
  }
  signalUrl(token) {
    const url = new URL(this.path + 'signal', this.http.base);
    url.protocol = 'wss:'; url.searchParams.set('token', token); return url.href;
  }
  async refresh() {
    if (this.closed) return;
    if (this.pending) return this.pending;
    this.pending = (async () => {
      try {
        const [config, host] = await Promise.all([this.request('config'), this.request('stats')]);
        if (!this.closed) this.events.publish({online: true, config, host, error: ''});
      } catch (error) {
        if (!this.closed) this.events.publish({...this.state, online: false, error: error.message});
      }
    })();
    try { await this.pending; }
    finally { this.pending = null; }
  }
  acquire() {
    if (this.closed) throw Error('AV client has been disposed');
    if (++this.users === 1) {
      this.refresh();
      this.timer = setInterval(() => { if (!this.document?.hidden) this.refresh(); }, 1000);
    }
    let released = false;
    return () => { if (!released) { released = true; if (--this.users === 0) clearInterval(this.timer); } };
  }
  dispose() { this.closed = true; clearInterval(this.timer); this.events.clear(); }
}

/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
import {apiBase, endpoint, snapshotAt, streamSelection} from './client-core.mjs';

const ages = ['age_seconds', 'acquisition_age_seconds', 'sample_age_seconds', 'progress_age_seconds'];

// A page shares this adapter across logical StreamClients. Changing demand
// replaces the one physical stream; no command or operation is ever replayed.
export class MultiplexSseTransport {
  constructor({base = '/', EventSource = globalThis.EventSource, now = () => performance.now(),
    schedule = (callback, delay) => globalThis.setTimeout(callback, delay),
    cancel = timer => globalThis.clearTimeout(timer)} = {}) {
    Object.assign(this, {base: apiBase(base), EventSource, now, schedule, cancel});
    this.members = new Set();
    this.source = null;
    this.timer = null;
    this.disposed = false;
    this.reset();
  }

  reset() {
    this.stream = null; this.sequence = 0;
    this.metadata = new Map(); this.samples = new Map();
    this.header = null;
  }

  emit(channel, event) {
    for (const member of [...this.members]) {
      if (this.members.has(member) && (!channel || member.channel === channel)) member.emit(event);
    }
  }

  open(selection, emit) {
    if (this.disposed) throw new Error('Transport has been disposed');
    const member = {...streamSelection(selection.channel, selection.topics), emit};
    this.members.add(member);
    this.queue(0);
    let closed = false;
    return () => {
      if (closed) return;
      closed = true;
      this.members.delete(member);
      if (!this.members.size) this.stop();
      else this.queue(0);
    };
  }

  stop() {
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = null;
    this.source?.close();
    this.source = null;
    this.reset();
  }

  queue(delay) {
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = this.schedule(() => { this.timer = null; this.connect(); }, delay);
  }

  connect() {
    this.stop();
    if (this.disposed || !this.members.size) return;
    this.emit(null, {type: 'disconnected', error: 'Establishing current stream selection'});
    // A synchronous subscriber can release the final owner during notification.
    if (this.disposed || !this.members.size) return;
    const url = endpoint(this.base, 'api/stream');
    const channels = new Set(), topics = new Set();
    for (const member of this.members) {
      channels.add(member.channel);
      for (const id of member.topics) topics.add(id);
    }
    for (const channel of [...channels].sort()) url.searchParams.append('channel', channel);
    for (const id of [...topics].sort()) url.searchParams.append('topic', id);
    let source;
    try { source = this.source = new this.EventSource(url.href); }
    catch (error) { this.emit(null, {type: 'disconnected', error: error.message}); this.queue(1000); return; }
    const active = () => this.source === source && !this.disposed;
    source.onopen = () => {
      if (!active()) return;
      this.reset();
      this.emit(null, {type: 'connected'});
    };
    source.onmessage = event => {
      if (!active()) return;
      try { this.accept(JSON.parse(event.data)); }
      catch (error) {
        this.stop();
        this.emit(null, {type: 'disconnected', error: `Invalid stream: ${error.message}`});
        if (this.members.size) this.queue(1000);
      }
    };
    source.onerror = () => {
      if (!active()) return;
      this.reset();
      this.emit(null, {type: 'disconnected', error: 'Observation connection interrupted'});
      if (source.readyState === (this.EventSource.CLOSED ?? 2)) this.queue(1000);
    };
  }

  accept(packet) {
    if (packet.version !== 2 || typeof packet.stream_id !== 'string' || !packet.stream_id ||
        packet.sequence !== this.sequence + 1 || (this.stream !== null && packet.stream_id !== this.stream)) {
      throw new Error('Missing baseline or invalid stream sequence');
    }
    this.stream = packet.stream_id; this.sequence = packet.sequence;
    for (const channel of ['runtime', 'resources']) {
      if (packet[channel]) this.emit(channel, {type: 'snapshot', snapshot: packet[channel]});
    }
    if (!packet.observations) return;
    const update = packet.observations, now = this.now();
    if (!Array.isArray(update.metadata) || !Array.isArray(update.updates) ||
        !Number.isFinite(update.sent_at) || typeof update.reset !== 'boolean' || !update.header) {
      throw new Error('Invalid observations');
    }
    if (!this.header && !update.reset) throw new Error('Observation baseline required');
    const oldHeader = this.header;
    if (update.reset) { this.metadata.clear(); this.samples.clear(); }
    for (const spec of update.metadata) {
      if (typeof spec.id !== 'string') throw new Error('Metadata needs a topic ID');
      this.metadata.set(spec.id, spec);
    }
    const changedTopics = new Set(update.metadata.map(value => value.id));
    for (const record of update.updates) {
      const sample = record.value, spec = this.metadata.get(sample?.id);
      if (!spec || !Number.isFinite(record.at)) throw new Error('Topic metadata or timestamp missing');
      const value = {...spec, ...sample}, elapsed = Math.max(0, update.sent_at - record.at);
      for (const key of ages) if (value[key] != null) value[key] += elapsed;
      this.samples.set(value.id, {value, at: now});
      changedTopics.add(value.id);
    }
    this.header = update.header;
    const headerChanged = ['config_revision', 'boot_id', 'generation', 'available', 'error'].some(
      key => oldHeader?.[key] !== this.header[key]);
    for (const member of [...this.members]) {
      if (member.channel !== 'observations' || !this.members.has(member)) continue;
      const topics = member.topics.flatMap(id => {
        const sample = this.samples.get(id);
        if (!sample) return [];
        return snapshotAt('observations', {...this.header, topics: [sample.value]},
          Math.max(0, (now - sample.at) / 1000), true).snapshot.topics;
      });
      member.emit({type: 'snapshot', snapshot: {...this.header, topics},
        changedTopics: update.reset || headerChanged ? null : [...changedTopics]});
    }
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.stop();
    this.emit(null, {type: 'disconnected', error: 'Transport disposed'});
    this.members.clear();
  }
}

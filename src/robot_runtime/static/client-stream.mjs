/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
import {snapshotAt, streamSelection} from './client-core.mjs';

// Page-owned, DOM-free state. The transport is a port, not an EventSource.
// Views subscribe to state; the page explicitly acquires and connects a stream.
export class StreamClient {
  constructor({channel, transport, now = () => performance.now(),
    schedule = (callback, delay) => globalThis.setTimeout(callback, delay),
    cancel = timer => globalThis.clearTimeout(timer), onListenerError = error => console.error(error)} = {}) {
    if (!['runtime', 'observations', 'resources'].includes(channel) || typeof transport?.open !== 'function') {
      throw new TypeError('A known channel and transport.open are required');
    }
    Object.assign(this, {channel, transport, now, schedule, cancel, onListenerError});
    this.listeners = new Set();
    this.owners = 0;
    this.wanted = false;
    this.selection = {channel, topics: []};
    this.connection = 'idle';
    this.error = null;
    this.source = null;
    this.receivedAt = null;
    this.baseline = false;
    this.generation = 0;
    this.notification = 0;
    this.opening = false;
    this.closeTransport = null;
    this.expiry = null;
    this.disposed = false;
  }

  get state() {
    const elapsed = this.receivedAt === null ? 0 : Math.max(0, (this.now() - this.receivedAt) / 1000);
    const {snapshot} = snapshotAt(this.channel, this.source, elapsed, this.baseline && this.connection === 'open');
    return {connection: this.connection, wanted: this.wanted, topics: [...this.selection.topics], snapshot, error: this.error};
  }

  checkOpen() { if (this.disposed) throw new Error('Stream client has been disposed'); }

  notify(listener, state = this.state) {
    try { listener(state); }
    catch (error) { this.onListenerError(error); }
  }

  publish(changedTopics = null) {
    const notification = ++this.notification, state = this.state;
    for (const listener of [...this.listeners]) {
      if (notification !== this.notification) return;
      if (this.listeners.has(listener) && (changedTopics === null || !listener.topicIds ||
          changedTopics.some(id => listener.topicIds.has(id)))) this.notify(listener, state);
    }
    if (notification !== this.notification) return;
    if (this.expiry !== null) this.cancel(this.expiry);
    this.expiry = null;
    if (!this.baseline || this.connection !== 'open') return;
    const elapsed = Math.max(0, (this.now() - this.receivedAt) / 1000);
    const {expiresIn} = snapshotAt(this.channel, this.source, elapsed, true);
    if (expiresIn !== null) {
      // A single deadline, not a rendering loop. state also computes current
      // freshness on every read if a background browser delays this timer.
      this.expiry = this.schedule(() => { this.expiry = null; this.publish(); }, Math.max(1, Math.ceil(expiresIn * 1000) + 1));
    }
  }

  subscribe(listener, {topics = null} = {}) {
    this.checkOpen();
    if (typeof listener !== 'function') throw new TypeError('A listener is required');
    const wrapped = state => listener(state);
    if (topics !== null) {
      if (this.channel !== 'observations') throw new TypeError('Topic filters need an observations client');
      wrapped.topicIds = new Set(streamSelection('observations', topics).topics);
    }
    this.listeners.add(wrapped);
    this.notify(wrapped);
    return () => this.listeners.delete(wrapped);
  }

  acquire() {
    this.checkOpen();
    this.owners++;
    this.start();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      if (!this.disposed && --this.owners === 0) this.disconnect();
    };
  }

  connect({topics = []} = {}) {
    this.checkOpen();
    const selection = streamSelection(this.channel, topics);
    const changed = JSON.stringify(selection) !== JSON.stringify(this.selection);
    if (changed) {
      this.stop();
      this.source = null;
      this.receivedAt = null;
      this.selection = selection;
    }
    this.wanted = true;
    if (this.owners) this.start();
    else this.publish();
  }

  start() {
    if (!this.wanted || !this.owners || this.closeTransport || this.opening || this.connection === 'connecting') return;
    const generation = ++this.generation;
    this.connection = 'connecting';
    this.baseline = false;
    this.error = null;
    this.publish();
    // A view may release/disconnect in its state listener.
    if (generation !== this.generation || !this.wanted || !this.owners) return;
    this.opening = true;
    try {
      const close = this.transport.open(this.selection, event => {
        if (this.disposed || generation !== this.generation) return;
        if (event.type === 'snapshot') {
          try {
            if (event.snapshot === null) throw new TypeError('Snapshot cannot be null');
            snapshotAt(this.channel, event.snapshot, 0, true);
            this.source = event.snapshot;
            this.receivedAt = this.now();
            this.baseline = true;
            this.connection = 'open';
            this.error = null;
          } catch (error) {
            this.baseline = false;
            this.connection = 'disconnected';
            this.error = error.message;
          }
        } else if (event.type === 'connected') {
          this.connection = 'open';
          // Connected transport is not evidence that an old sample is current.
          this.baseline = false;
        } else if (event.type === 'disconnected') {
          this.connection = 'disconnected';
          this.baseline = false;
          this.error = event.error || 'Observation connection interrupted';
        } else return;
        this.publish(event.type === 'snapshot' && this.baseline ? event.changedTopics ?? null : null);
      });
      if (typeof close !== 'function') throw new TypeError('Transport must return a close function');
      if (generation === this.generation && !this.disposed) this.closeTransport = close;
      else close();
    } catch (error) {
      if (generation !== this.generation) return;
      this.connection = 'disconnected';
      this.error = error.message;
      this.publish();
    } finally {
      this.opening = false;
      // A synchronous adapter callback can replace selection while open() is
      // still returning. Close that old session before starting the new one.
      if (generation !== this.generation && !this.disposed) this.start();
    }
  }

  stop() {
    this.generation++;
    const close = this.closeTransport;
    this.closeTransport = null;
    close?.();
    if (this.expiry !== null) this.cancel(this.expiry);
    this.expiry = null;
    this.baseline = false;
    this.connection = 'idle';
  }

  disconnect() {
    if (this.disposed) return;
    this.wanted = false;
    this.stop();
    this.publish();
  }

  dispose() {
    if (this.disposed) return;
    this.wanted = false;
    this.stop();
    this.disposed = true;
    this.connection = 'disposed';
    this.publish();
    this.listeners.clear();
  }
}

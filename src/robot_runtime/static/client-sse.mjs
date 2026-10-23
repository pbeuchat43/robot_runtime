/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
import {apiBase, endpoint, streamSelection} from './client-core.mjs';

const paths = {runtime: 'api/events', observations: 'api/ros/events', resources: 'api/resources/events'};

// An adapter emits connected, snapshot, and disconnected events. It owns only
// read-only reconnection. Commands always go through the separate HTTP client.
export class SseTransport {
  constructor({base = '/', EventSource = globalThis.EventSource,
    schedule = (callback, delay) => globalThis.setTimeout(callback, delay),
    cancel = timer => globalThis.clearTimeout(timer)} = {}) {
    this.base = apiBase(base);
    Object.assign(this, {EventSource, schedule, cancel});
  }

  open(selection, emit) {
    const {channel, topics} = streamSelection(selection.channel, selection.topics);
    const url = endpoint(this.base, paths[channel]);
    for (const topic of topics) url.searchParams.append('topic', topic);
    let source = null, retry = null, closed = false;
    const reconnect = () => {
      if (closed || retry !== null) return;
      retry = this.schedule(() => { retry = null; connect(); }, 1000);
    };
    const connect = () => {
      if (closed) return;
      let current;
      try { current = new this.EventSource(url.href); }
      catch (error) { emit({type: 'disconnected', error: error.message}); reconnect(); return; }
      source = current;
      const active = () => !closed && source === current;
      current.onopen = () => { if (active()) emit({type: 'connected'}); };
      current.onmessage = event => {
        if (!active()) return;
        let snapshot;
        try { snapshot = JSON.parse(event.data); }
        catch { emit({type: 'disconnected', error: 'Invalid observation JSON'}); return; }
        emit({type: 'snapshot', snapshot});
      };
      current.onerror = () => {
        if (!active()) return;
        emit({type: 'disconnected', error: 'Observation connection interrupted'});
        // Native EventSource owns interrupted-connection retries. An HTTP
        // admission failure can leave it CLOSED permanently, requiring a retry.
        if (current.readyState === (this.EventSource.CLOSED ?? 2)) reconnect();
      };
    };
    connect();
    return () => {
      if (closed) return;
      closed = true;
      if (retry !== null) this.cancel(retry);
      source?.close();
      source = null;
    };
  }
}

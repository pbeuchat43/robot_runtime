/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
import {apiBase, endpoint} from './client-core.mjs';

export class RequestError extends Error {
  constructor(message, {code = 'transport_error', status = null, requestId = null, outcomeUnknown = false} = {}) {
    super(message);
    this.name = 'RequestError';
    Object.assign(this, {code, status, requestId, outcomeUnknown});
  }
}

// Shared explicit reads/commands. Authentication remains owned by the host page
// and robot; headers() can provide the current CSRF token. No request is retried.
export class HttpClient {
  constructor({base = '/', fetcher = (...args) => globalThis.fetch(...args), headers = () => ({})} = {}) {
    this.base = apiBase(base);
    this.fetcher = fetcher;
    this.headers = headers;
    this.pending = new Set();
    this.disposed = false;
  }

  async request(path, {method = 'GET', body, timeoutMs = 5000, signal} = {}) {
    if (this.disposed) throw new Error('HTTP client has been disposed');
    if (!['GET', 'POST'].includes(method) || !Number.isFinite(timeoutMs) || timeoutMs <= 0 || (method === 'GET' && body !== undefined)) {
      throw new TypeError('Use GET or POST with a positive timeout; GET has no body');
    }
    const url = endpoint(this.base, path);
    const headers = new Headers(this.headers());
    const encoded = body === undefined ? undefined : JSON.stringify(body);
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    signal?.addEventListener('abort', abort, {once: true});
    this.pending.add(controller);
    const timer = setTimeout(abort, timeoutMs);
    let dispatched = false;
    try {
      if (controller.signal.aborted) throw new Error('Request cancelled before dispatch');
      dispatched = true;
      const response = await this.fetcher(url, {method, headers, credentials: 'same-origin', cache: 'no-store',
        ...(encoded === undefined ? {} : {body: encoded}), signal: controller.signal});
      const result = await response.json();
      if (controller.signal.aborted) throw new Error('Request cancelled');
      if (!response.ok) {
        const code = result.error?.code || 'http_error';
        throw new RequestError(result.error?.message || `Request failed (${response.status})`, {
          code, status: response.status, requestId: body?.request_id ?? null,
          outcomeUnknown: method === 'POST' && (response.status >= 500 || ['transport_unknown', 'outcome_unknown'].includes(code)),
        });
      }
      return result;
    } catch (error) {
      if (error instanceof RequestError) throw error;
      throw new RequestError(controller.signal.aborted ? 'Request cancelled or timed out' : error.message, {
        requestId: body?.request_id ?? null, outcomeUnknown: method === 'POST' && dispatched,
      });
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      this.pending.delete(controller);
    }
  }

  get(path, options) { return this.request(path, {...options, method: 'GET'}); }
  post(path, body, options) { return this.request(path, {...options, method: 'POST', body}); }
  operation(requestId, options) { return this.get(`api/operations/${encodeURIComponent(requestId)}`, options); }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    for (const controller of this.pending) controller.abort();
  }
}

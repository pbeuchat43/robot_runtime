/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
import {HttpClient, RequestError} from '/assets/client-http.mjs';
import {SseTransport} from '/assets/client-sse.mjs';
import {StreamClient} from '/assets/client-stream.mjs';
import {MultiplexSseTransport} from '/assets/client-multiplex.mjs';
import {runAV} from '/av-suite.mjs';

const assert = (condition, message) => { if (!condition) throw new Error(message); };
const equal = (actual, expected, message) => assert(JSON.stringify(actual) === JSON.stringify(expected), message);
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function rejected(promise, check) {
  try { await promise; } catch (error) { assert(check(error), `Unexpected error: ${error}`); return error; }
  throw new Error('Expected rejection');
}
function throws(callback) {
  try { callback(); } catch { return; }
  throw new Error('Expected exception');
}
async function until(check) {
  const end = performance.now() + 2500;
  while (!(await check())) {
    assert(performance.now() < end, 'Condition timed out');
    await new Promise(resolve => setTimeout(resolve, 25));
  }
}
class Clock {
  time = 0;
  next = 0;
  tasks = new Map();
  now = () => this.time;
  schedule = (callback, delay) => { const id = ++this.next; this.tasks.set(id, {at: this.time + delay, callback}); return id; };
  cancel = id => this.tasks.delete(id);
  advance(milliseconds) {
    this.time += milliseconds;
    for (const [id, task] of [...this.tasks]) {
      if (task.at <= this.time) { this.tasks.delete(id); task.callback(); }
    }
  }
}
class Transport {
  sessions = [];
  open(selection, emit) {
    const session = {selection, emit, closes: 0};
    this.sessions.push(session);
    return () => session.closes++;
  }
  send(snapshot) { this.sessions.at(-1).emit({type: 'snapshot', snapshot}); }
}
const topic = (id = 'cones', overrides = {}) => ({id, fresh: true, status: 'current',
  age_seconds: 0, stale_after_seconds: 1, sequence: '9007199254740993',
  data: {stamp_ns: '1792497600123456789', points: []}, ...overrides});
const observations = (topics = [topic()]) => ({api_version: 1, available: true,
  boot_id: 'boot-a', generation: 3, topics, parameters: [{id: 'catalog-only'}]});
function fixture(channel = 'observations') {
  const clock = new Clock(), transport = new Transport();
  const client = new StreamClient({channel, transport, now: clock.now, schedule: clock.schedule, cancel: clock.cancel});
  const connect = () => client.connect(channel === 'observations' ? {topics: ['cones']} : {});
  return {clock, transport, client, connect};
}

export async function run() {
  const checks = [];
  async function test(name, body) {
    try { await body(); checks.push({name, passed: true}); }
    catch (error) { checks.push({name, passed: false, error: error.stack || String(error)}); }
  }
  await test('inert construction, explicit intent, shared ownership and teardown', () => {
    const {clock, transport, client, connect} = fixture();
    const unwatch = client.subscribe(() => {});
    const releaseA = client.acquire(), releaseB = client.acquire();
    equal([transport.sessions.length, clock.tasks.size], [0, 0], 'Observing/acquiring must be inert');
    connect(); connect();
    equal(transport.sessions.length, 1, 'Owners share one connection');
    transport.send(observations());
    releaseA(); releaseA();
    equal(transport.sessions[0].closes, 0, 'Another owner remains');
    releaseB();
    equal([transport.sessions[0].closes, clock.tasks.size, client.state.wanted, client.state.snapshot.topics[0].fresh],
      [1, 0, false, false], 'Last release stops owned work and invalidates retained samples');
    const releaseC = client.acquire();
    equal(transport.sessions.length, 1, 'Reacquire does not restore intent');
    connect(); unwatch(); unwatch(); client.dispose(); client.dispose(); releaseC();
    equal([transport.sessions[1].closes, client.state.connection, clock.tasks.size], [1, 'disposed', 0], 'Disposal is idempotent');
    throws(connect); throws(() => client.acquire()); throws(() => client.subscribe(() => {}));
    const deferred = fixture('runtime');
    let intent = false;
    deferred.client.subscribe(state => { intent = state.wanted; });
    deferred.connect();
    assert(intent, 'Subscribers see connection intent before an owner acquires');
    equal(deferred.transport.sessions.length, 0, 'Intent alone creates no demand');
    deferred.client.acquire()();
    equal(deferred.transport.sessions[0].closes, 1, 'Acquire honors prior explicit intent');
  });
  await test('selection replaces baseline, missing/empty data, obsolete callbacks and lossless integers', () => {
    const {client, transport} = fixture();
    const release = client.acquire();
    client.connect({topics: ['lidar', 'cones', 'cones']});
    equal(transport.sessions[0].selection.topics, ['cones', 'lidar'], 'Selection normalized');
    client.connect({topics: ['cones', 'lidar']});
    equal(transport.sessions.length, 1, 'Equivalent selection is stable');
    transport.send(observations([topic(), topic('lidar')]));
    assert(!('parameters' in client.state.snapshot), 'Catalog is excluded from observation state');
    equal([client.state.snapshot.topics[0].sequence, client.state.snapshot.topics[0].data.stamp_ns],
      ['9007199254740993', '1792497600123456789'], 'Integer strings retained');
    transport.send(observations([]));
    equal(client.state.snapshot.topics, [], 'Missing topics replace previous collection');
    client.connect({topics: ['imu']});
    equal(client.state.snapshot, null, 'Selection change clears baseline');
    transport.sessions[0].emit({type: 'snapshot', snapshot: observations()});
    equal(client.state.snapshot, null, 'Obsolete callback ignored');
    transport.send(observations([topic('imu')]));
    equal(client.state.snapshot.topics[0].data.points, [], 'Valid empty sample retained');
    throws(() => client.connect({topics: []}));
    release();
  });
  await test('receipt/acquisition/sample deadlines, delayed timers and reconnect invalidation', () => {
    const {clock, client, transport, connect} = fixture();
    const release = client.acquire(); connect();
    let notifications = 0;
    client.subscribe(() => notifications++);
    transport.send(observations([topic('cones', {acquisition_age_seconds: 0.9})]));
    const before = notifications;
    clock.advance(101);
    assert(!client.state.snapshot.topics[0].fresh && notifications > before, 'Acquisition deadline emits stale state');
    transport.send(observations([topic('imu', {sample_age_seconds: 0.5})]));
    clock.time += 500;
    assert(!client.state.snapshot.topics[0].fresh, 'Strict sample boundary survives a delayed browser timer');
    transport.send(observations());
    transport.sessions[0].emit({type: 'disconnected'});
    assert(!client.state.snapshot.topics[0].fresh, 'Disconnect invalidates immediately');
    transport.sessions[0].emit({type: 'connected'});
    assert(!client.state.snapshot.topics[0].fresh, 'Open cannot renew a retained sample');
    transport.send(observations());
    clock.advance(1001);
    assert(!client.state.snapshot.topics[0].fresh, 'Receipt expires without further traffic');
    transport.send({...observations(), available: false});
    assert(!client.state.snapshot.topics[0].fresh, 'Unavailable source cannot be fresh');
    for (const invalid of [null, {api_version: 2, topics: []}, {topics: null}]) {
      transport.send(invalid);
      assert(client.state.connection === 'disconnected' && !client.state.snapshot.topics[0].fresh, 'Malformed baseline invalidates state');
    }
    release();
  });
  await test('runtime and resource freshness stay distinct from process/sample data', () => {
    for (const [channel, limit] of [['runtime', 15], ['resources', 3]]) {
      const {clock, client, transport, connect} = fixture(channel);
      const release = client.acquire(); connect();
      transport.send({api_version: 1, fresh: true, age_seconds: 0, stale_after_seconds: limit,
        units: [{id: 'policy', state: 'active', fresh: true, observation_age_seconds: 2}]});
      clock.advance(limit * 1000 + 1);
      assert(!client.state.snapshot.fresh, `${channel} expires`);
      if (channel === 'runtime') {
        assert(!client.state.snapshot.units[0].fresh, 'Unit evidence expires with runtime evidence');
        equal(client.state.snapshot.units[0].state, 'active', 'Do not invent a process transition');
      }
      release();
    }
  });
  await test('inactive model status requires a current observation connection', () => {
    const {client, transport, connect} = fixture();
    const release = client.acquire(); connect();
    transport.send(observations([topic('cones', {status: 'inactive', fresh: false, data: null, age_seconds: null})]));
    equal(client.state.snapshot.topics[0].status, 'inactive', 'Live run explicitly excludes this model');
    transport.sessions[0].emit({type: 'disconnected'});
    equal(client.state.snapshot.topics[0].status, 'disconnected', 'Lost connection cannot keep asserting the selected mode');
    transport.sessions[0].emit({type: 'connected'});
    equal(client.state.snapshot.topics[0].status, 'disconnected', 'Reconnect needs a new baseline');
    transport.send(observations());
    assert(client.state.snapshot.topics[0].fresh, 'New active model baseline replaces inactive state');
    release();
  });
  await test('listener isolation and synchronous transport lifecycle changes', () => {
    const {client, transport, connect} = fixture();
    let errors = 0, received = 0;
    client.onListenerError = () => errors++;
    client.subscribe(() => { throw new Error('broken view'); });
    client.subscribe(() => received++);
    const release = client.acquire(); connect(); transport.send(observations());
    assert(errors === received && received > 1, 'One view cannot prevent other notifications');
    release();
    const sessions = [];
    const sync = new StreamClient({channel: 'observations', transport: {
      open(selection, emit) {
        const session = {selection, closes: 0}; sessions.push(session);
        emit({type: 'snapshot', snapshot: observations()});
        return () => session.closes++;
      },
    }});
    sync.subscribe(state => { if (state.snapshot && state.topics[0] === 'cones') sync.connect({topics: ['lidar']}); });
    sync.acquire(); sync.connect({topics: ['cones']});
    equal([sessions.length, sessions[0].closes, sync.state.topics], [2, 1, ['lidar']], 'Reentrant replacement closes old adapter');
    sync.dispose();
    const closing = new StreamClient({channel: 'runtime', transport: {
      open(selection, emit) { emit({type: 'connected'}); return () => received++; },
    }});
    closing.subscribe(state => { if (state.connection === 'open') closing.dispose(); });
    closing.acquire(); closing.connect();
    equal(closing.state.connection, 'disposed', 'Synchronous disposal stays final');
  });
  await test('SSE routing, native/closed retries, malformed JSON and cancelled callbacks', () => {
    const clock = new Clock(), sources = [], events = [];
    class Source {
      static CLOSED = 2;
      readyState = 0;
      constructor(url) { this.url = url; sources.push(this); }
      close() { this.closed = true; }
    }
    const adapter = new SseTransport({base: `${location.origin}/robot/`, EventSource: Source, schedule: clock.schedule, cancel: clock.cancel});
    equal(sources.length, 0, 'Constructor is inert');
    const close = adapter.open({channel: 'observations', topics: ['b', 'a']}, event => events.push(event));
    equal(sources[0].url, `${location.origin}/robot/api/ros/events?topic=a&topic=b`, 'Mount prefix and selection preserved');
    sources[0].onerror(); equal(clock.tasks.size, 0, 'Native retry owns CONNECTING state');
    sources[0].readyState = 2; sources[0].onerror(); sources[0].onerror();
    equal(clock.tasks.size, 1, 'Permanent closure has one retry');
    clock.advance(1000); equal(sources.length, 2, 'Closed source replaced');
    const count = events.length;
    sources[0].onmessage({data: '{}'});
    equal(events.length, count, 'Replaced source ignored');
    sources[1].onmessage({data: 'invalid'});
    equal(events.at(-1).type, 'disconnected', 'Malformed JSON invalidates state');
    sources[1].readyState = 2; sources[1].onerror(); close(); close(); clock.advance(1000);
    equal([sources.length, sources[1].closed, clock.tasks.size], [2, true, 0], 'Close cancels retry');
    const nativeTimers = new SseTransport({EventSource: Source});
    const cancelRetry = nativeTimers.open({channel: 'runtime'}, () => {});
    sources.at(-1).readyState = 2; sources.at(-1).onerror(); cancelRetry();
    assert(sources.at(-1).closed, 'Native browser timer callbacks retain their required receiver');
  });
  await test('multiplexed channels, per-topic ages, filtered views and reconnect baseline', () => {
    const clock = new Clock(), sources = [];
    class Source {
      static CLOSED = 2;
      constructor(url) { this.url = url; this.readyState = 1; sources.push(this); }
      close() { this.closed = true; }
    }
    const transport = new MultiplexSseTransport({EventSource: Source, now: clock.now, schedule: clock.schedule, cancel: clock.cancel});
    const clients = Object.fromEntries(['runtime', 'observations', 'resources'].map(channel => [channel,
      new StreamClient({channel, transport, now: clock.now, schedule: clock.schedule, cancel: clock.cancel})]));
    const releases = Object.values(clients).map(client => client.acquire());
    equal(sources.length, 0, 'Acquisition remains inert');
    clients.runtime.connect(); clients.resources.connect(); clients.observations.connect({topics: ['a', 'b']});
    clock.advance(0);
    equal(sources.length, 1, 'Three logical channels share one EventSource');
    const source = sources[0]; source.onopen();
    const url = new URL(source.url);
    equal(url.searchParams.getAll('channel'), ['observations', 'resources', 'runtime'], 'Logical channel union');
    let bRenders = 0;
    clients.observations.subscribe(() => bRenders++, {topics: ['b']});
    const send = (sequence, updates) => source.onmessage({data: JSON.stringify({version: 2, stream_id: 'wire-1', sequence, ...updates})});
    const metadata = ['a', 'b'].map(id => ({id, stale_after_seconds: 1, fields: {data: 'int64'}}));
    send(1, {observations: {reset: true, sent_at: 100, header: {available: true, generation: 'worker'},
      metadata, updates: ['a', 'b'].map(id => ({at: 100, value: topic(id)}))}});
    assert(clients.observations.state.snapshot.topics.every(value => value.fresh), 'Initial topic baseline');
    const count = bRenders;
    clock.time = 900;
    send(2, {observations: {reset: false, sent_at: 100.9, header: {available: true, generation: 'worker'},
      metadata: [], updates: [{at: 100.9, value: topic('a', {sequence: 2, data: {points: []}})}]}});
    equal(bRenders, count, 'An a update does not render b');
    const b = clients.observations.state.snapshot.topics.find(value => value.id === 'b');
    assert(Math.abs(b.age_seconds - .9) < .001, 'Unchanged topic age was not reset');
    equal(b.data.stamp_ns, '1792497600123456789', 'Integer strings stay lossless');
    clock.advance(110);
    const values = clients.observations.state.snapshot.topics;
    assert(values[0].fresh && !values[1].fresh && bRenders > count, 'Independent expiry notifies the affected view');
    send(4, {}); // gap must invalidate every channel and require a full new stream.
    assert(source.closed && clients.observations.state.connection === 'disconnected', 'Gapped sequence fails closed');
    clock.advance(1000);
    equal(sources.length, 2, 'Read-only reconnection is bounded');
    sources[1].onopen();
    assert(!clients.observations.state.snapshot.topics.some(value => value.fresh), 'Opening cannot revive previous samples');
    sources[1].onmessage({data: JSON.stringify({version: 2, stream_id: 'wire-2', sequence: 1,
      observations: {reset: true, sent_at: 200, header: {available: true, generation: 'new-worker'},
        metadata: [metadata[0]], updates: [{at: 200, value: topic('a', {data: {points: []}, run_id: 'new-run'})}]}})});
    equal(clients.observations.state.snapshot.topics.map(value => value.id), ['a'], 'New baseline removes absent old topics');
    releases.forEach(release => release());
    Object.values(clients).forEach(client => client.dispose()); transport.dispose();
    equal(clock.tasks.size, 0, 'Last release/disposal leaves no connection or expiry timers');
    assert(sources.every(value => value.closed), 'All physical connections are closed');
    const inert = new MultiplexSseTransport({EventSource: Source, schedule: clock.schedule, cancel: clock.cancel});
    const sourceCount = sources.length;
    let close = inert.open({channel: 'runtime'}, () => close());
    clock.advance(0);
    equal([sources.length, clock.tasks.size], [sourceCount, 0], 'Releasing during reconnect notification cannot open an orphan stream');
    inert.dispose();
    const filtered = fixture();
    filtered.client.acquire(); filtered.connect();
    filtered.transport.send(observations([topic('cones')]));
    let lastFresh = true;
    filtered.client.subscribe(value => { lastFresh = value.snapshot.topics[0].fresh; }, {topics: ['cones']});
    filtered.transport.sessions.at(-1).emit({type: 'snapshot', snapshot: {}, changedTopics: ['unrelated']});
    assert(!lastFresh, 'An invalid update invalidates even a filtered subscriber');
    filtered.client.dispose();
  });
  await test('HTTP explicit requests, CSRF, mount boundary and command uncertainty without replay', async () => {
    const requests = [];
    let csrf = 'first', response = {ok: true, status: 200, json: async () => ({accepted: true})};
    const http = new HttpClient({base: `${location.origin}/robot/`, headers: () => ({'X-CSRF-Token': csrf}),
      fetcher: async (url, options) => { requests.push({url: url.href, options}); if (response instanceof Error) throw response; return response; }});
    equal(requests.length, 0, 'HTTP constructor is inert');
    const body = {request_id: 'request-1', expected_run_id: 'run-1', arguments: {value: '9007199254740993'}};
    await http.post('api/command', body);
    equal([requests[0].url, requests[0].options.headers.get('X-CSRF-Token'), requests[0].options.credentials,
      JSON.parse(requests[0].options.body)], [`${location.origin}/robot/api/command`, 'first', 'same-origin', body], 'Command envelope retained');
    csrf = 'second'; await http.operation('request-1');
    equal([requests[1].options.method, requests[1].options.headers.get('X-CSRF-Token')], ['GET', 'second'], 'Lookup is an explicit read with current CSRF');
    for (const path of ['../api/command', '/api/command', 'https://elsewhere.invalid/api', 'api\\command']) {
      await rejected(http.post(path, body), error => error instanceof TypeError);
    }
    equal(requests.length, 2, 'Invalid paths never dispatch');
    for (const [status, code, unknown] of [[403, 'forbidden', false], [503, 'unavailable', true], [409, 'outcome_unknown', true]]) {
      response = {ok: false, status, json: async () => ({error: {code, message: code}})};
      await rejected(http.post('api/command', body), error => error instanceof RequestError &&
        error.status === status && error.code === code && error.outcomeUnknown === unknown && error.requestId === body.request_id);
    }
    response = new Error('lost reply');
    await rejected(http.post('api/command', body), error => error.outcomeUnknown);
    response = {ok: true, status: 200, json: async () => { throw new Error('invalid JSON'); }};
    await rejected(http.post('api/command', body), error => error.outcomeUnknown);
    equal(requests.length, 7, 'Commands never replay automatically');
    http.dispose();
    await rejected(http.get('api/status'), error => /disposed/.test(error.message));
  });
  await test('HTTP cancellation before dispatch, in flight, timeout and disposal', async () => {
    let dispatched = 0;
    const http = new HttpClient({fetcher: async (url, {signal}) => {
      dispatched++;
      return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true}));
    }});
    const signal = new AbortController(); signal.abort();
    await rejected(http.post('api/command', {request_id: 'cancelled'}, {signal: signal.signal}), error => !error.outcomeUnknown);
    equal(dispatched, 0, 'Pre-cancelled command never sent');
    await rejected(http.post('api/command', {request_id: 'timed-out'}, {timeoutMs: 10}), error => error.outcomeUnknown);
    const controller = new AbortController();
    const pending = http.post('api/command', {request_id: 'in-flight'}, {signal: controller.signal});
    controller.abort();
    await rejected(pending, error => error.outcomeUnknown);
    const read = http.get('api/status'); http.dispose(); http.dispose();
    await rejected(read, error => !error.outcomeUnknown);
    equal(http.pending.size, 0, 'All request bookkeeping released');
  });
  await test('real EventSource/fetch loads selected packaged modules and releases demand', async () => {
    const http = new HttpClient({headers: () => ({'X-CSRF-Token': 'fixture-only'})});
    const client = new StreamClient({channel: 'observations', transport: new SseTransport()});
    const release = client.acquire();
    let sawFresh = false, sawExpiry = false;
    const unwatch = client.subscribe(state => {
      if (state.snapshot?.topics[0]?.fresh) sawFresh = true;
      else if (sawFresh) sawExpiry = true;
    });
    try {
      client.connect({topics: ['cones']});
      await until(() => client.state.snapshot?.topics[0]?.fresh);
      const metrics = await http.get('fixture/metrics');
      equal([metrics.active, metrics.selections], [1, [['cones']]], 'Real server receives one selected stream');
      const body = {request_id: 'fixture-command', arguments: {value: '9007199254740993'}};
      const echo = await http.post('fixture/command', body);
      equal([echo.body, echo.csrf], [body, 'fixture-only'], 'Real fetch preserves JSON and CSRF');
      await until(() => sawExpiry);
      assert(!client.state.snapshot.topics[0].fresh, 'Expiry notifies views without polling state');
      release(); client.dispose();
      await until(async () => (await http.get('fixture/metrics')).active === 0);
    } finally { unwatch(); release(); client.dispose(); http.dispose(); }
  });
  await runAV(test);
  await tick();
  return {passed: checks.every(check => check.passed), checks};
}

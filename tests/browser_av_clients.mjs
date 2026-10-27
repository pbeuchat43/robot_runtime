// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul N. Beuchat
import {observable, neutralInput} from '/assets/av/core.js';
import {RuntimeAVApi} from '/assets/av/runtime-api.js';
import {MediaClient} from '/assets/av/media-client.js';
import {ControlClient} from '/assets/av/control-client.js';
import {GamepadInput} from '/assets/av/gamepad-input.js';
import {mountGamepad} from '/assets/av/components/gamepad-view.js';
import {mountControl} from '/assets/av/components/control-panel.js';
import {mountVideo} from '/assets/av/components/video-view.js';
import {changeLaunch} from '/assets/launch-control.mjs';
import {mountSourceControls} from '/assets/av/components/source-controls.js';
import {mountCameraPicker} from '/assets/av/components/camera-picker.js';

const assert = (condition, message) => { if (!condition) throw Error(message); };
const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
const settle = () => new Promise(resolve => setTimeout(resolve, 80));

async function renewableMedia() {
  let at = 1000, nextToken = 0;
  const calls = [], config = {settings: {change_id: 0, applying: false}, ice_servers: [], stamp: false};
  const api = {state: {online: true, host: {settings: config.settings}}, async request(route, body, options) {
    calls.push({route, body, options});
    if (route === 'media/sessions') return {token: `token-${++nextToken}`, ttl_s: 20, profile: 'observer'};
    if (route === 'media/renew') return api.renew?.();
    if (route === 'config') return config;
  }};
  const media = new MediaClient({api, now: () => at, signallingUrl: 'wss://fixture.invalid', signallingFactory: async () => ({close() {},
    api: {registerConnectionListener() {}, registerPeerListener() {}, getAvailableProducers: () => []}})});
  media.attachVideo({srcObject: null, cancelVideoFrameCallback() {}});
  await media.connect();
  return {media, api, calls, time(value) { at = value; }};
}

export async function runAV(test) {
  await test('transient media renewal retries the same subscription without dropping its transport', async () => {
    const {media, api, calls, time} = await renewableMedia(), signalling = media.signalling;
    try {
      api.renew = () => { throw Object.assign(Error('Network timeout'), {code: 'transport_error'}); };
      time(8000); await media.tick();
      assert(media.want && signalling && media.signalling === signalling, 'A single network failure keeps the current viewer');
      time(8500); await media.tick();
      assert(calls.filter(c => c.route === 'media/renew').length === 1, 'Retry is paced');
      api.renew = () => ({}); time(9000); await media.tick();
      assert(media.want && media.leaseUntil === 29000, 'Confirmed renewal extends the lease from request dispatch');
      assert(calls.filter(c => c.route === 'media/renew').every(c => c.body.token === 'token-1' && c.options.timeout <= 3000),
        'Renewals retain their token and use a short timeout');
      assert(calls.filter(c => c.route === 'media/sessions').length === 1 && !calls.some(c => c.route === 'media/close'),
        'No new admission or subscription close');
    } finally { media.dispose(); }
  });
  await test('unconfirmed media renewal stops at its deadline including browser suspension', async () => {
    for (const suspended of [false, true]) {
      const {media, api, calls, time} = await renewableMedia();
      try {
        api.renew = () => { throw Object.assign(Error('Offline'), {code: 'transport_error'}); };
        if (!suspended) {
          time(8000); await media.tick(); time(20500); await media.tick();
          assert(calls.filter(c => c.route === 'media/renew').at(-1).options.timeout === 500, 'Timeout fits remaining lease');
        }
        const renewals = calls.filter(c => c.route === 'media/renew').length;
        time(21000); await media.tick(); time(22000); await media.tick();
        assert(!media.want && media.state.error.includes('deadline expired'), 'Expiry requires explicit Connect');
        assert(calls.filter(c => c.route === 'media/renew').length === renewals, 'No renewal at or after expiry');
        assert(calls.filter(c => c.route === 'media/sessions').length === 1, 'No automatic admission');
      } finally { media.dispose(); }
    }
  });
  await test('media authorization and source rejections disconnect immediately', async () => {
    for (const code of ['unauthorized', 'forbidden', 'stale_run', 'stale_source', 'expired']) {
      const {media, api, calls, time} = await renewableMedia();
      try {
        api.renew = () => { throw Object.assign(Error(code), {code}); };
        time(8000); await media.tick();
        assert(!media.want && media.state.error.includes(code), `${code}: no retry of authority rejection`);
        assert(calls.filter(c => c.route === 'media/close').length === 1, 'Rejected subscription is closed');
      } finally { media.dispose(); }
    }
  });
  await test('obsolete media renewal completion cannot change a new connection', async () => {
    for (const failed of [false, true]) {
      const {media, api, time} = await renewableMedia(); let finish;
      try {
        api.renew = () => new Promise((resolve, reject) => { finish = () => failed ?
          reject(Object.assign(Error('Old rejection'), {code: 'expired'})) : resolve({}); });
        time(8000); const oldTick = media.tick();
        media.disconnect(); time(9000); await media.connect();
        const deadline = media.leaseUntil;
        finish(); await oldTick;
        assert(media.want && media.token === 'token-2' && media.leaseUntil === deadline && !media.state.error,
          `Old ${failed ? 'failure' : 'success'} changed the new subscription: ${JSON.stringify({want: media.want,
            token: media.token, deadline, actualDeadline: media.leaseUntil, error: media.state.error})}`);
      } finally { media.dispose(); }
    }
  });
  await test('overlay switching revokes video without restarting the detector owner', async () => {
    const root = document.body.appendChild(document.createElement('div')), calls = [];
    const config = {source_selection: {id: 'cones', revision: 4, choices: [
      {id: 'cones', label: 'Cones', available: true}, {id: 'coco', label: 'COCO 80', available: true}]},
      runtime: {source_run: 'unchanged'}, settings: {applying: false, change_id: 0}};
    const events = observable({online: true, config, host: {settings: config.settings, camera: {
      devices: [], selected: 'cones', discovery: {}, warnings: [], state: 'idle'}}});
    const api = {get state() { return events.state; }, subscribe: fn => events.subscribe(fn), acquire: () => () => {},
      request: async (route, body) => calls.push({route, body}), refresh: async () => {}};
    const media = new MediaClient({api}); media.config = config;
    const picker = mountCameraPicker(root, {api, media, declared: true});
    try {
      const device = root.querySelector('[data-ref=device]');
      device.value = 'coco'; device.dispatchEvent(new Event('change')); await settle();
      assert(calls.some(c => c.route === 'camera' && c.body.id === 'coco' && c.body.source_revision === 4), 'Selection bound to observed source revision');
      assert(calls.every(c => !c.route.includes('start') && !c.route.includes('stop')), 'No detector lifecycle operation');
      media.want = true;
      media.backend({online: true, config: {...config, source_selection: {...config.source_selection, id: 'coco', revision: 5}}, host: {}});
      assert(!media.want && media.state.error.includes('Video source changed'), 'Other viewer must explicitly reconnect');
      assert(!calls.some(c => c.route === 'media/sessions'), 'No admission replay');
      events.publish({...events.state, config: {...config, source_selection: {...config.source_selection,
        choices: config.source_selection.choices.map(c => ({...c, available: c.id === 'cones'}))}}});
      assert(device.options[1].disabled, 'Inactive model cannot be selected');
    } finally { picker.destroy(); media.dispose(); root.remove(); }
  });
  await test('explicit video reconnect awaits a fresh binding even when hidden-tab polling paused', async () => {
    let releaseConfig;
    const gate = new Promise(resolve => { releaseConfig = resolve; }), posts = [];
    const config = {runtime: {run_id: 'video', source_run: 'new-source', generation: 'generation', config_revision: 'revision'},
      server_time_ms: Date.now(), settings: {change_id: 0, applying: false}, ice_servers: [], stamp: false};
    const http = {base: location.href, async get(path) {
      if (path.endsWith('/config')) { await gate; return {...config}; }
      return {settings: config.settings};
    }, async post(path, body) { posts.push({path, body}); return {token: 'token', ttl_s: 3, profile: 'observer'}; }};
    const api = new RuntimeAVApi({http, capability: 'video', document: {hidden: true}});
    api.events.publish({online: true, config: {...config, runtime: {...config.runtime, source_run: 'old-source'}}, host: {settings: config.settings}});
    const media = new MediaClient({api, document: {hidden: true}, signallingFactory: async () => ({close() {},
      api: {registerConnectionListener() {}, registerPeerListener() {}, getAvailableProducers: () => []}})});
    const detach = media.attachVideo({srcObject: null, cancelVideoFrameCallback() {}}), release = media.acquire();
    try {
      const connecting = media.connect();
      await Promise.resolve(); assert(posts.length === 0, 'No admission request before pending refresh completes');
      releaseConfig(); await connecting;
      assert(posts[0]?.body.source_run === 'new-source', 'Explicit Connect uses the new source run');
      assert(media.want && media.config.runtime.source_run === 'new-source', 'Refresh does not revoke the explicit new connection');
      media.backend({config: {...config, runtime: {...config.runtime, source_run: 'third-source'}}, host: {}, online: true});
      assert(!media.want, 'Subsequent source change still revokes the active session without replay');
    } finally { releaseConfig(); detach(); release(); media.dispose(); api.dispose(); }
  });
  await test('selected restart requires confirmed cleanup and never continues after uncertainty or cancellation', async () => {
    for (const scenario of ['success', 'unknown', 'timeout', 'replaced', 'cancel', 'preflight-failed']) {
      const commands = [], controller = new AbortController(); let prepares = 0, disconnected = 0;
      const http = {
        async post(path, body) {
          if (path.endsWith('prepare-launch')) {
            prepares++;
            if (scenario === 'preflight-failed') throw Error('Missing executable');
            return {config_revision: 'rev', stop_timeout_seconds: 1,
              status: {fresh: true, server_time_ms: 1000, units: [{unit_id: 'worker',
                run_id: scenario === 'replaced' && prepares === 2 ? 'other' : 'old',
                blocked: prepares === 1, ownership: 'owned'}]}};
          }
          commands.push({path, body});
          if (path.endsWith('/stop')) return {request_id: 'stop-id', phase: 'in_progress'};
          return {request_id: 'start-id', phase: 'in_progress'};
        },
        async operation() {
          if (scenario === 'cancel') controller.abort();
          return {request_id: 'stop-id', phase: scenario === 'unknown' ? 'outcome_unknown' : scenario === 'timeout' ? 'timed_out' : 'complete', outcome: 'cgroup_empty'};
        },
      };
      let error = null;
      try { await changeLaunch({http, unitId: 'worker', revision: 'rev', mode: 'b-objects', action: 'restart',
        expectedRun: 'old', signal: controller.signal, disconnect: () => disconnected++, sleep: async () => {}, uuid: () => 'fixture'}); }
      catch (value) { error = value; }
      assert(Boolean(error) === (scenario !== 'success'), `${scenario}: expected sequence outcome`);
      assert(commands.filter(item => item.path.endsWith('/start')).length === (scenario === 'success' ? 1 : 0), `${scenario}: no unconfirmed Start`);
      assert(disconnected === (scenario === 'preflight-failed' ? 0 : 1), `${scenario}: preflight before disconnect`);
      if (scenario === 'success') {
        assert(commands[0].body.expected_run === 'old' && commands[1].body.mode === 'b-objects', 'Exact old run and selected new mode');
      }
    }
  });
  await test('independent source selectors keep requested and resolved configurations separate', async () => {
    const root = document.body.appendChild(document.createElement('div'));
    const view = mountSourceControls(root, {http: {}, disconnect() {}});
    const unit = {unit_id: 'worker', available: true, blocked: true, ownership: 'owned', lifecycle: 'running', run_id: 'old',
      default_launch_mode: 'a-cones', launch_mode: {label: 'A cones'},
      launch_selections: [{id: 'camera', label: 'Camera', choices: [{id: 'a', label: 'A'}, {id: 'b', label: 'B'}]},
        {id: 'model', label: 'Model', choices: [{id: 'cones', label: 'Cones'}, {id: 'objects', label: 'Objects'}]}],
      launch_modes: ['a', 'b'].flatMap(camera => ['cones', 'objects'].map(model => ({id: `${camera}-${model}`,
        label: `${camera} ${model}`, selections: {camera, model}})))};
    try {
      view.update({unit, snapshot: {fresh: true, config_revision: 'rev'}, enabled: true});
      const camera = root.querySelector('[data-selection=camera]'), model = root.querySelector('[data-selection=model]');
      camera.value = 'b'; camera.dispatchEvent(new Event('change'));
      model.value = 'objects'; model.dispatchEvent(new Event('change'));
      assert(root.querySelector('[data-ref=requested]').textContent.includes('b objects'), 'Both independent choices resolve');
      assert(root.querySelector('[data-ref=resolved]').textContent.includes('A cones'), 'Selection does not relabel current run');
      assert(!root.querySelector('[data-ref=restart]').disabled && root.querySelector('[data-ref=start]').disabled, 'Running selection requires restart');
      view.update({unit, snapshot: {fresh: false, config_revision: 'rev'}, enabled: true});
      assert(root.querySelector('[data-ref=restart]').disabled, 'Stale ownership disables restart');
      view.update({unit: {...unit, launch_selections: [], default_launch_mode: null}, requiredMode: 'b-objects',
        snapshot: {fresh: true, config_revision: 'rev'}, enabled: true});
      assert(root.querySelector('[data-ref=requested]').textContent.includes('b objects'), 'Legacy required mode is respected');
    } finally { view.destroy(); root.remove(); }
  });
  await test('AV packets sample the device without animation frames; timeout and focus still pause', () => {
    let at = 1000, focused = true, reads = 0;
    const device = {index: 0, id: 'fixture', mapping: 'standard', connected: true,
      axes: [.7, 0, 0, 0], buttons: Array.from({length: 17}, () => ({value: 0}))};
    const document = {hidden: false, hasFocus: () => focused};
    const input = new GamepadInput({navigator: {getGamepads: () => { reads++; return [device]; }},
      document, now: () => at, requestFrame: () => 1, cancelFrame() {}});
    const packets = [], control = new ControlClient({input, api: {}, document, now: () => at});
    control.policy = {input_max_age_ms: 250, input_timeout_ms: 250};
    control.channel = {readyState: 'open', bufferedAmount: 0, send: text => packets.push(JSON.parse(text))};
    control.lease = {epoch: 'a'.repeat(32), lease: 1}; control.leaseAt = at;
    control.ackAt = at; control.rtt = 0;
    assert(control.enable(), 'Explicit enable succeeds');
    control.pulse();
    assert(reads === 1 && packets.at(-1).active && packets.at(-1).axes[0] === .7,
      'Packet reads the actual device even when no paint callback has run');
    device.axes[0] = -.5; at += 50; control.pulse();
    assert(reads === 2 && packets.at(-1).axes[0] === -.5, 'Next packet replaces the previous device sample');
    at += 201; control.pulse();
    assert(!control.enabled && !packets.at(-1).active && control.state.reason.includes('acknowledgement overdue'),
      'The unchanged 250 ms deadline pauses and reports its actual cause');
    control.ackAt = at; control.leaseAt = at; control.pulse();
    assert(!control.enabled, 'Fresh receipts cannot automatically re-enable');
    control.enable(); focused = false; control.pulse();
    assert(!control.enabled && !packets.at(-1).active, 'Focus loss still deactivates');
    input.dispose();
  });

  await test('AV controller rendering is deferred, skips unchanged DOM, and releases pending paint', async () => {
    const root = document.body.appendChild(document.createElement('div'));
    const state = {...neutralInput(), devices: [{index: 0, id: 'Fixture pad', mapping: 'standard'}],
      selected: 0, focused: true, active: true, simulation: false, error: ''};
    const input = {events: observable(state), get state() { return this.events.state; },
      subscribe(fn) { return this.events.subscribe(fn); }, acquire: () => () => {}, select() {}, simulate() {}};
    const view = mountGamepad(root, {input});
    try {
      await settle();
      const mutations = [], observer = new MutationObserver(list => mutations.push(...list));
      observer.observe(root, {childList: true, characterData: true, subtree: true});
      input.events.publish({...state}); await settle();
      assert(mutations.length === 0, 'Identical neutral input does not replace text or device options');
      const previous = root.querySelector('[data-ref=left-stick]').getAttribute('transform');
      input.events.publish({...state, axes: [.8, 0, 0, 0]});
      assert(root.querySelector('[data-ref=left-stick]').getAttribute('transform') === previous,
        'Publishing input does not synchronously render');
      await settle();
      assert(root.querySelector('[data-ref=steering]').textContent === '78%', 'Next paint shows the latest stick');
      assert(root.querySelector('[data-ref=axes]').textContent === '', 'Closed raw diagnostics do no text work');
      root.querySelector('details').open = true;
      input.events.publish({...state, axes: [.8, 0, 0, 0]}); await settle();
      assert(root.querySelector('[data-ref=axes]').textContent.includes('0: 0.800'), 'Open diagnostics still update');
      observer.disconnect();
      input.events.publish(state); view.destroy(); await frame();
      assert(root.childNodes.length === 0, 'Destroy cancels pending paint');
    } finally { view.destroy(); root.remove(); }
  });

  await test('AV receipt and video views preserve unchanged text and immediately reflect pause', async () => {
    const controlState = {status: 'connected', enabled: true, canEnable: true, reason: 'Enabled',
      receiptFresh: true, ackAge: 0, rtt: 5, ack: {seq: 1, active: true, source: 'gamepad', preview: {steering: 0, throttle: 0, brake: 0}}};
    const control = {events: observable(controlState), acquire: () => () => {},
      subscribe(fn) { return this.events.subscribe(fn); }};
    const mediaState = {status: 'connected', profile: 'observer', error: '', report: {fps: 5, width: 640, height: 360, mbps: .2},
      freshness: {arriving: true, fresh: false, age: null, reason: 'Observer'}};
    const media = {events: observable(mediaState), config: {stamp: false}, acquire: () => () => {}, attachVideo: () => () => {},
      subscribe(fn) { return this.events.subscribe(fn); }};
    const root = document.body.appendChild(document.createElement('div'));
    const receipt = root.appendChild(document.createElement('div')), video = root.appendChild(document.createElement('div'));
    const views = [mountControl(receipt, {control}), mountVideo(video, {media})];
    try {
      const mutations = [], observer = new MutationObserver(list => mutations.push(...list));
      observer.observe(root, {childList: true, characterData: true, subtree: true});
      for (let i = 0; i < 10; i++) { control.events.publish({...controlState}); media.events.publish({...mediaState}); }
      await Promise.resolve();
      assert(mutations.length === 0, 'Repeated receipts and freshness polls do not replace identical text');
      control.events.publish({...controlState, enabled: false, reason: 'Paused', receiptFresh: false,
        ack: {...controlState.ack, active: false, reason: 'paused'}});
      assert(!receipt.querySelector('[data-ref=enabled]').checked && receipt.querySelector('[data-ref=preview]').textContent.includes('inactive'),
        'Pause status and inactive preview update without waiting for the metrics throttle');
      observer.disconnect();
    } finally { views.forEach(view => view.destroy()); root.remove(); }
  });
}

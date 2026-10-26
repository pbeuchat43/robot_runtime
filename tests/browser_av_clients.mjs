// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul N. Beuchat
import {observable, neutralInput} from '/assets/av/core.js';
import {ControlClient} from '/assets/av/control-client.js';
import {GamepadInput} from '/assets/av/gamepad-input.js';
import {mountGamepad} from '/assets/av/components/gamepad-view.js';
import {mountControl} from '/assets/av/components/control-panel.js';
import {mountVideo} from '/assets/av/components/video-view.js';
import {changeLaunch} from '/assets/launch-control.mjs';
import {mountSourceControls} from '/assets/av/components/source-controls.js';

const assert = (condition, message) => { if (!condition) throw Error(message); };
const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
const settle = () => new Promise(resolve => setTimeout(resolve, 80));

export async function runAV(test) {
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

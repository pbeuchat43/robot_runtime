// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul N. Beuchat
import {observable, neutralInput} from '/assets/av/core.js';
import {ControlClient} from '/assets/av/control-client.js';
import {GamepadInput} from '/assets/av/gamepad-input.js';
import {mountGamepad} from '/assets/av/components/gamepad-view.js';
import {mountControl} from '/assets/av/components/control-panel.js';
import {mountVideo} from '/assets/av/components/video-view.js';

const assert = (condition, message) => { if (!condition) throw Error(message); };
const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
const settle = () => new Promise(resolve => setTimeout(resolve, 80));

export async function runAV(test) {
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

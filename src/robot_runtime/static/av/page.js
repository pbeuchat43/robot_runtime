// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul N. Beuchat
import {createPageClients} from '../client-page.mjs';
import {mountRuntime, mountObservations} from '../views-runtime.mjs';
import {RuntimeAVApi} from './runtime-api.js';
import {GamepadInput} from './gamepad-input.js';
import {ControlClient} from './control-client.js';
import {MediaClient} from './media-client.js';
import {mountGamepad} from './components/gamepad-view.js';
import {mountControl} from './components/control-panel.js';
import {mountVideo} from './components/video-view.js';
import {mountStreamSettings} from './components/stream-settings.js';
import {mountSourceControls} from './components/source-controls.js';

const mode = location.pathname.slice(1), $ = id => document.getElementById(id);
const wantsMedia = mode !== 'input', wantsInput = mode !== 'video';
document.body.dataset.combined = String(wantsMedia && wantsInput);
document.title = `${wantsMedia && wantsInput ? 'Camera and gamepad' : wantsMedia ? 'Video' : 'Gamepad input'} · Robot runtime`;
$('title').textContent = document.title;
$('media-section').hidden = !wantsMedia; $('input-section').hidden = !wantsInput;
let csrf = null, catalog = null, snapshot = null, disposed = false, refreshing = false, ros = null;
const clients = createPageClients({headers: () => csrf ? {'X-CSRF-Token': csrf} : {}});
const releaseRuntime = clients.runtime.acquire();
const runtimeView = mountRuntime($('runtime'), clients);
const input = new GamepadInput(), compositions = new Map();
const sourceControls = mountSourceControls($('source-controls'), {http: clients.http,
  disconnect: () => compositions.get('media')?.client.disconnect()});
let observationsView = null, observationsKey = '';
const releaseObservations = wantsMedia ? clients.observations.acquire() : () => {};
if (wantsInput) compositions.set('local', {views: [mountGamepad($('gamepad'), {input})]});

function clear(kind) {
  const value = compositions.get(kind);
  if (!value) return;
  for (const view of value.views) view.destroy();
  value.client?.dispose(); value.api?.dispose(); compositions.delete(kind);
}
function mount(kind) {
  clear(kind);
  const capability = $(kind + '-select').value;
  if (!capability) return;
  const api = new RuntimeAVApi({http: clients.http, capability});
  const client = kind === 'media' ? new MediaClient({api}) : new ControlClient({api, input});
  const views = kind === 'media' ? [mountVideo($('video'), {media: client}),
    mountStreamSettings($('settings'), {api})] : [mountControl($('receipt'), {control: client})];
  compositions.set(kind, {api, client, views}); renderServices();
}
function renderServices() {
  for (const kind of ['media', 'input']) {
    const service = catalog?.services.find(s => s.id === $(kind + '-select').value);
    const unit = snapshot?.units.find(u => u.unit_id === service?.unit_id);
    $(kind + '-start').disabled = !csrf || !unit || !snapshot.fresh || !['exited', 'stopped', 'failed'].includes(unit.lifecycle);
    $(kind + '-stop').disabled = !csrf || !unit || !snapshot.fresh || unit.ownership !== 'owned' || !unit.run_id || Boolean(unit.stop_request_id);
    $(kind + '-status').textContent = service ? `${service.label} · ${unit?.lifecycle || 'unknown'} · ${service.source?.label || 'Diagnostic input only'}` : 'No declared service';
  }
  const service = catalog?.services.find(s => s.id === $('media-select').value);
  const owner = service?.source?.owner_unit;
  const unit = snapshot?.units.find(u => u.unit_id === owner);
  sourceControls.update({unit, snapshot, enabled: Boolean(csrf), requiredMode: service?.source?.required_mode});
  const topics = wantsMedia ? (ros?.topics || []).filter(topic => topic.unit_id === owner && !topic.projection) : [];
  const key = JSON.stringify(topics.map(topic => [topic.id, topic.label]));
  if (key !== observationsKey) {
    observationsKey = key; observationsView?.destroy();
    observationsView = mountObservations($('source-observations'), {observations: clients.observations, topics, title: 'Processing state'});
    if (topics.length) clients.observations.connect({topics: topics.map(topic => topic.id)});
    else clients.observations.disconnect();
  }
}
async function refresh() {
  if (refreshing || disposed) return;
  refreshing = true;
  try {
    const [session, next, nextRos] = await Promise.all([clients.http.get('api/session'), clients.http.get('api/av'),
      snapshot?.ros?.enabled ? clients.http.get('api/ros') : Promise.resolve(null)]);
    if (disposed) return;
    csrf = session.csrf; catalog = next; ros = nextRos;
    $('sign-in').hidden = session.authenticated;
    $('auth-status').textContent = session.authenticated ? 'Commands unlocked' : 'Unlock to start services and connect';
    $('lock').disabled = !session.authenticated;
    for (const kind of ['media', 'input']) {
      const select = $(kind + '-select'), previous = select.value;
      const services = catalog.services.filter(s => s.kind === kind);
      const signature = JSON.stringify(services.map(s => [s.id, s.label]));
      if (select.dataset.signature !== signature) {
        select.dataset.signature = signature;
        select.replaceChildren(...services.map(s => new Option(s.label, s.id)));
        if (services.some(s => s.id === previous)) select.value = previous;
        if ((kind === 'media' ? wantsMedia : wantsInput) && (!compositions.has(kind) || select.value !== previous)) mount(kind);
      }
    }
    renderServices();
  } catch (error) { $('message').textContent = error.message; }
  finally { refreshing = false; }
}
$('unlock').addEventListener('submit', async event => {
  event.preventDefault();
  const passphrase = $('passphrase').value; $('passphrase').value = '';
  try {
    await clients.http.post('api/login', {passphrase}); $('message').textContent = ''; await refresh();
  } catch (error) { $('message').textContent = error.message; }
});
$('lock').addEventListener('click', async () => {
  sourceControls.cancel();
  for (const value of compositions.values()) value.client?.disconnect();
  try { await clients.http.post('api/logout', {}); await refresh(); }
  catch (error) { $('message').textContent = error.message; }
});
for (const kind of ['media', 'input']) {
  $(kind + '-select').addEventListener('change', () => mount(kind));
  for (const action of ['start', 'stop']) $(kind + '-' + action).addEventListener('click', async () => {
    const service = catalog?.services.find(s => s.id === $(kind + '-select').value);
    const unit = snapshot?.units.find(u => u.unit_id === service?.unit_id);
    if (!unit) return;
    const button = $(kind + '-' + action); button.disabled = true;
    if (action === 'stop') compositions.get(kind)?.client.disconnect();
    try {
      const clock = await clients.http.get('api/status');
      const request = {request_id: crypto.randomUUID(), expires_at_ms: clock.server_time_ms + 10000,
        config_revision: snapshot.config_revision, ...(action === 'stop' ? {expected_run: unit.run_id} : {})};
      const result = await clients.http.post(`api/units/${unit.unit_id}/${action}`, request);
      $('message').textContent = `${action} accepted (${result.request_id}). Runtime state below reports the outcome.`;
    } catch (error) { $('message').textContent = error.message; }
    finally { renderServices(); }
  });
}
const unwatch = clients.runtime.subscribe(state => { snapshot = state.snapshot; renderServices(); });
clients.runtime.connect(); refresh();
const timer = setInterval(() => { if (!document.hidden) refresh(); }, 3000);
window.addEventListener('pagehide', event => {
  sourceControls.cancel(); clients.observations.disconnect();
  for (const value of compositions.values()) value.client?.disconnect();
  clients.runtime.disconnect();
  if (event.persisted) return;
  disposed = true; clearInterval(timer); unwatch(); runtimeView.destroy();
  for (const kind of [...compositions.keys()]) clear(kind);
  sourceControls.destroy(); observationsView?.destroy(); releaseObservations();
  input.dispose(); releaseRuntime(); clients.dispose();
});
window.addEventListener('pageshow', () => {
  if (!disposed) { clients.runtime.connect(); observationsKey = ''; renderServices(); }
});

/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
import {createPageClients} from './client-page.mjs';
import {mountRuntime, mountObservations} from './views-runtime.mjs';

// Minimal compositions demonstrate reuse. Detailed scenario UI and driving
// controls remain separate work. Selection is explicit, public and read-only.
const mode = document.body.dataset.composition;
const clients = createPageClients();
const releaseRuntime = clients.runtime.acquire(), releaseObservations = clients.observations.acquire();
const runtimeView = mountRuntime(document.querySelector('#runtime'), clients);
let observationViews = [], selected = [], wanted = false, disposed = false;
const button = document.querySelector('#observe'), note = document.querySelector('#note');
const applicationIds = new Set(['policy-state-code', 'policy-state-text', 'traxxas-state-code', 'traxxas-state-text']);
function sync() {
  if (wanted && !document.hidden && selected.length) clients.observations.connect({topics: selected});
  else clients.observations.disconnect();
  button.textContent = wanted ? 'Pause observations' : 'Observe selected topics';
}
button.addEventListener('click', () => { wanted = !wanted; sync(); });
document.addEventListener('visibilitychange', sync);
let catalogKey = null, loading = false, again = false;
async function catalog() {
  if (loading) { again = true; return; }
  loading = true;
  try {
    const value = await clients.http.get('api/ros');
    if (disposed) return;
    for (const view of observationViews) view.destroy();
    observationViews = [];
    const application = value.topics.filter(topic => applicationIds.has(topic.id));
    observationViews.push(mountObservations(document.querySelector('#application'), {
      observations: clients.observations, topics: application, title: 'Application state'
    }));
    let sensors = [];
    if (mode === 'outreach') {
      sensors = value.topics.filter(topic => !applicationIds.has(topic.id));
      observationViews.push(mountObservations(document.querySelector('#sensors'), {
        observations: clients.observations, topics: sensors, title: 'Sensor and diagnostic samples'
      }));
    }
    selected = [...application, ...sensors].map(topic => topic.id);
    button.disabled = !selected.length;
    note.textContent = `${selected.length} selected topics. Observing does not start workloads. Hidden pages release observation demand.`;
    sync();
  } catch (error) { note.textContent = error.message; }
  finally {
    loading = false;
    if (again && !disposed) { again = false; catalog(); }
  }
}
const unwatch = clients.runtime.subscribe(({snapshot}) => {
  if (!snapshot) return;
  const key = JSON.stringify([snapshot.config_revision, snapshot.ros?.generation]);
  if (catalogKey !== key) { catalogKey = key; catalog(); }
});
clients.runtime.connect();
window.addEventListener('pagehide', event => {
  clients.runtime.disconnect(); clients.observations.disconnect();
  if (event.persisted) return;
  disposed = true;
  unwatch(); runtimeView.destroy();
  for (const view of observationViews) view.destroy();
  releaseRuntime(); releaseObservations(); clients.dispose();
});
window.addEventListener('pageshow', () => { if (!disposed) { clients.runtime.connect(); sync(); } });

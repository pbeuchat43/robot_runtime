// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul N. Beuchat
import {scope} from '../core.js';
import {changeLaunch} from '../../launch-control.mjs';

export function mountSourceControls(root, {http, disconnect}) {
  const ui = scope(root, `<section class="robot-widget"><h3>Source workload</h3>
    <div data-ref="choices" class="robot-settings-grid"></div>
    <p data-ref="requested" class="robot-small"></p><p data-ref="resolved" class="robot-small robot-mono"></p>
    <div class="robot-actions"><button data-ref="start">Start source</button>
    <button data-ref="restart">Restart with selection</button><button data-ref="stop">Stop source</button></div>
    <p data-ref="message" class="robot-small" role="status"></p></section>`);
  let state = {}, signature = '', controls = [], selected = null, controller = null, alive = true;
  function render() {
    if (!alive) return;
    const {unit, snapshot, enabled, requiredMode} = state;
    root.hidden = !unit;
    if (!unit) return;
    const nextSignature = JSON.stringify([unit.unit_id, snapshot.config_revision, requiredMode, unit.launch_selections, unit.launch_modes]);
    if (signature !== nextSignature) {
      controller?.abort(); signature = nextSignature;
      controls = (unit.launch_selections || []).map(selection => {
        const label = document.createElement('label'), select = document.createElement('select');
        select.dataset.selection = selection.id;
        label.append(document.createTextNode(selection.label), select);
        select.replaceChildren(...selection.choices.map(choice => new Option(choice.label, choice.id)));
        const initial = unit.launch_modes.find(mode => mode.id === unit.default_launch_mode)?.selections;
        if (initial?.[selection.id]) select.value = initial[selection.id];
        select.addEventListener('change', render);
        return {id: selection.id, select, label};
      });
      ui.get('choices').replaceChildren(...controls.map(item => item.label));
    }
    selected = controls.length ? unit.launch_modes.find(mode => controls.every(item => mode.selections?.[item.id] === item.select.value)) :
      unit.launch_modes.find(mode => mode.id === (requiredMode || unit.default_launch_mode));
    const valid = (!controls.length || Boolean(selected)) && (!requiredMode || selected?.id === requiredMode), busy = Boolean(controller);
    for (const item of controls) item.select.disabled = busy;
    ui.text('requested', `Requested: ${selected?.label || (controls.length ? 'Unsupported combination' : 'Default launch')}`);
    ui.text('resolved', `Current run: ${unit.launch_mode?.label || 'Default / none'} · ${unit.lifecycle} · ${snapshot.fresh ? unit.ownership : 'stale ownership'}${unit.run_id ? ` · ${unit.run_id}` : ''}`);
    const allowed = enabled && snapshot.fresh && unit.available && !busy;
    ui.get('start').disabled = !allowed || !valid || unit.blocked;
    ui.get('restart').disabled = !allowed || !valid || !unit.blocked || unit.ownership !== 'owned' || Boolean(unit.stop_request_id);
    ui.get('stop').disabled = !allowed || !unit.blocked || unit.ownership !== 'owned' || Boolean(unit.stop_request_id);
  }
  for (const action of ['start', 'restart', 'stop']) ui.on(ui.get(action), 'click', async () => {
    if (controller || ui.get(action).disabled) return;
    const {unit, snapshot} = state, mode = selected?.id;
    controller = new AbortController(); render();
    try {
      const operation = await changeLaunch({http, unitId: unit.unit_id, revision: snapshot.config_revision,
        mode, action, expectedRun: unit.run_id, signal: controller.signal, disconnect,
        progress: text => ui.text('message', text)});
      if (alive) ui.text('message', `${action}: ${operation.phase} (${operation.request_id}). Connect video after the source is ready.`);
    } catch (error) {
      if (alive) ui.text('message', `${error.message}${error.requestId ? ` Request ${error.requestId}.` : ''}`);
    } finally { controller = null; render(); }
  });
  return {update(next) { state = next; render(); }, cancel() { controller?.abort(); },
    destroy() { alive = false; controller?.abort(); ui.destroy(); }};
}

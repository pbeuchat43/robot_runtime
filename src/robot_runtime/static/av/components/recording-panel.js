// Copyright (c) 2026 Paul N. Beuchat. MIT License.
import {changeLaunch} from '../../launch-control.mjs';

export function mountRecording(root, {http}) {
  root.innerHTML = `<h2>Record on robot</h2><p class="robot-small">Recording continues when this page closes or the live view changes.</p>
    <fieldset><legend>Save video</legend>
      <label class="robot-check"><input type="checkbox" value="rgb" checked> Camera RGB</label>
      <label class="robot-check"><input type="checkbox" value="annotated"> Annotated</label>
      <label class="robot-check"><input type="checkbox" value="depth"> Colourized depth</label>
      <label>Recording overlay <select aria-label="Recording overlay"></select></label>
      <label class="robot-check"><input type="checkbox" value="index" checked> Include frame index for capture matching</label>
    </fieldset><div class="robot-actions"><button data-action="start">Start recording</button> <button data-action="stop">Stop recording</button></div>
    <p data-status class="robot-small" role="status"></p><p data-location class="robot-small robot-mono"></p><pre data-outputs></pre><p data-message class="robot-small" role="status"></p>`;
  const fieldset = root.querySelector('fieldset'), select = root.querySelector('select');
  const start = root.querySelector('[data-action="start"]'), stop = root.querySelector('[data-action="stop"]');
  let current = {}, pending = false;
  const render = () => {
    const {snapshot, enabled} = current, config = snapshot?.recording;
    root.hidden = !config;
    if (!config) return;
    const unit = snapshot.units.find(x => x.unit_id === config.unit_id);
    const source = snapshot.units.find(x => x.unit_id === config.source_unit_id);
    const overlays = config.overlays_by_mode[source?.launch_mode?.id] || [];
    const signature = JSON.stringify(overlays);
    if (select.dataset.signature !== signature) {
      const previous = select.value; select.dataset.signature = signature;
      select.replaceChildren(...overlays.map(x => new Option(x === 'coco' ? 'COCO' : x[0].toUpperCase() + x.slice(1), x)));
      if (overlays.includes(previous)) select.value = previous;
    }
    fieldset.disabled = pending || Boolean(unit?.blocked);
    const annotated = root.querySelector('input[value="annotated"]');
    annotated.disabled = !overlays.length; if (!overlays.length) annotated.checked = false;
    select.disabled = !annotated.checked;
    const hasOutput = [...fieldset.querySelectorAll('input:checked')].some(x => x.value !== 'index');
    start.disabled = pending || !enabled || !snapshot.fresh || !unit || unit.blocked || !hasOutput || source?.lifecycle !== 'running' || Boolean(source?.stop_request_id);
    stop.disabled = pending || !enabled || !snapshot.fresh || !unit?.blocked || unit.ownership !== 'owned' || Boolean(unit.stop_request_id);
    const state = unit?.recording;
    const label = value => (value || '').replaceAll('_', ' ');
    root.querySelector('[data-status]').textContent = `${label(state?.state) || 'Not started'}${state?.finalization ? ' · files: ' + label(state.finalization) : ''}${state?.bytes ? ' · ' + state.bytes + ' bytes' : ''}${state?.fresh === false ? ' · observation unavailable or stale' : ''}${state?.failure ? ' · ' + state.failure : ''}`;
    root.querySelector('[data-location]').textContent = state?.output_directory ? `Saved to ${state.output_directory}` : '';
    root.querySelector('[data-outputs]').textContent = Object.entries(state?.outputs || {}).map(([name, track]) =>
      `${name}: ${label(track.state)} · received ${track.counters.received} · admitted ${track.counters.admitted} · encoded ${track.counters.encoded} · finalized ${track.counters.finalized}\n  rate drops ${track.counters.rate_dropped} · overload drops ${track.counters.overload_dropped} · rejected ${track.counters.rejected} · gaps ${track.counters.gaps}${track.failure ? '\n' + track.failure : ''}`).join('\n');
  };
  fieldset.addEventListener('change', render);
  const command = async action => {
    const {snapshot} = current, config = snapshot?.recording;
    if (!config || pending) return;
    const unit = snapshot.units.find(x => x.unit_id === config.unit_id);
    const outputs = [...fieldset.querySelectorAll('input:checked')].map(x => x.value).filter(x => x !== 'index');
    const recording = {outputs, frame_index: root.querySelector('input[value="index"]').checked,
      ...(outputs.includes('annotated') ? {overlay: select.value} : {})};
    pending = true; render();
    const message = root.querySelector('[data-message]');
    try {
      const result = await changeLaunch({http, unitId: config.unit_id, revision: snapshot.config_revision,
        action, expectedRun: unit.run_id, startOptions: {recording}, progress: text => {message.textContent = text;}});
      message.textContent = `${action === 'start' ? 'Start' : 'Stop'} accepted (${result.request_id}). File finalization is reported above.`;
    } catch (error) {message.textContent = error.message;}
    finally {pending = false; render();}
  };
  start.addEventListener('click', () => command('start')); stop.addEventListener('click', () => command('stop'));
  return {update(value) {current = value; render();}, destroy() {root.replaceChildren();}};
}

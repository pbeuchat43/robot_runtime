import {scope} from '../core.js';

export function mountStreamSettings(root, {api, control} = {}) {
  const ui = scope(root, `<section class="robot-widget"><details><summary data-ref="summary">Stream settings</summary>
    <form data-ref="form"><fieldset data-ref="fields" disabled><legend>Requested output</legend><div class="robot-settings-grid">
    <label>Resolution<select data-ref="resolution" required></select></label><label>Frame rate<select data-ref="fps" required></select></label>
    <label>Bitrate mode<select data-ref="mode"><option value="adaptive">Adaptive</option><option value="fixed">Fixed target</option></select></label>
    <label><span data-ref="maximum-label">Maximum video bitrate (Mbit/s)</span><input data-ref="maximum" type="number" min="0.05" max="20" step="any" required></label></div>
    <p class="robot-small">Adaptive video can reduce bitrate and resolution to fit the connection. Video bitrate excludes network and recovery overhead.</p>
    <details data-ref="advanced"><summary>Advanced</summary><div class="robot-settings-grid">
    <label><span data-ref="initial-label">Initial bitrate (Mbit/s)</span><input data-ref="initial" type="number" min="0.05" max="20" step="any" required></label>
    <label>Minimum bitrate (Mbit/s)<input data-ref="minimum" type="number" min="0.05" max="20" step="any" required></label>
    <label>Keyframe interval (seconds)<input data-ref="keyframe" type="number" min="0.25" max="5" step="any" required></label></div>
    <p class="robot-small">Minimum ≤ initial/fixed target ≤ maximum. Minimum applies to adaptive mode. H.264 and low-latency encoder tuning are retained.</p></details></fieldset>
    <p class="robot-small">Disconnect all video viewers before Apply. These settings apply to this media service for its current run. Connect video again when ready.</p>
    <div class="robot-actions"><button data-ref="apply" type="submit" disabled>Apply settings</button>
    <button data-ref="reset" type="button" disabled>Use current settings</button></div><p data-ref="status" role="status"></p></form></details></section>`);
  let current = null, revision = null, dirty = false, posting = false, requestError = '', alive = true;
  function fill() {
    const value = current.applied;
    ui.get('resolution').replaceChildren(...current.options.resolutions.map(([w, h]) => new Option(`${w} × ${h}`, `${w}x${h}`)));
    ui.get('fps').replaceChildren(...current.options.frame_rates.map(fps => new Option(`${fps} FPS`, fps)));
    ui.get('resolution').value = `${value.width}x${value.height}`; ui.get('fps').value = value.fps; ui.get('mode').value = value.mode;
    for (const [name, key] of [['maximum', 'max_bitrate'], ['initial', 'bitrate'], ['minimum', 'min_bitrate']]) {
      ui.get(name).value = value[key] / 1e6; ui.get(name).min = current.options.bitrate_min / 1e6; ui.get(name).max = current.options.bitrate_max / 1e6;
    }
    ui.get('keyframe').value = value.keyframe_interval; revision = current.revision; dirty = false;
  }
  function render() {
    if (!alive) return;
    if (!api.state.online || !current) {
      ui.get('fields').disabled = ui.get('apply').disabled = ui.get('reset').disabled = true;
      ui.get('status').textContent = 'Stream settings unavailable · check the Jetson connection'; return;
    }
    const busy = posting || current.applying, controlsOn = control?.state.enabled || current.controls_paused === false;
    const changed = revision !== current.revision, fixed = ui.get('mode').value === 'fixed';
    ui.get('fields').disabled = busy || current.failed;
    ui.get('initial-label').textContent = fixed ? 'Fixed target (Mbit/s)' : 'Initial bitrate (Mbit/s)';
    ui.get('maximum-label').textContent = fixed ? 'Maximum allowed target (Mbit/s)' : 'Maximum video bitrate (Mbit/s)';
    ui.get('apply').disabled = busy || controlsOn || !dirty || changed || current.failed;
    ui.get('reset').disabled = busy || (!dirty && !requestError && !changed);
    ui.get('apply').textContent = busy ? 'Applying…' : 'Apply settings';
    const value = current.applied;
    ui.get('summary').textContent = `Stream settings · ${value.width} × ${value.height} · ${value.fps} FPS · ${value.mode}`;
    const error = requestError || current.error;
    ui.get('status').classList.toggle('robot-error', Boolean(error || changed));
    ui.get('status').textContent = current.applying ? 'Applying settings… connect video again when ready.' :
      controlsOn ? 'Pause controls in every page before applying settings.' : changed ?
      'Another page changed the settings. Use current settings before applying your changes.' : error ||
      (dirty ? 'Changes have not been applied.' : 'Settings applied for this server run.');
  }
  for (const name of ['resolution', 'fps', 'mode', 'maximum', 'initial', 'minimum', 'keyframe']) {
    ui.on(ui.get(name), 'input', () => { dirty = true; requestError = ''; if (name === 'mode' && ui.get('mode').value === 'fixed') ui.get('advanced').open = true; render(); });
  }
  ui.on(ui.get('reset'), 'click', () => { requestError = ''; fill(); render(); });
  ui.on(ui.get('form'), 'submit', async event => {
    event.preventDefault();
    if (!current || posting || current.applying || current.failed || revision !== current.revision ||
        control?.state.enabled || current.controls_paused === false || !ui.get('form').reportValidity()) return;
    const [width, height] = ui.get('resolution').value.split('x').map(Number);
    const settings = {width, height, fps: Number(ui.get('fps').value), mode: ui.get('mode').value,
      max_bitrate: Math.round(Number(ui.get('maximum').value) * 1e6), bitrate: Math.round(Number(ui.get('initial').value) * 1e6),
      min_bitrate: Math.round(Number(ui.get('minimum').value) * 1e6), keyframe_interval: Number(ui.get('keyframe').value)};
    if (!(settings.min_bitrate <= settings.bitrate && settings.bitrate <= settings.max_bitrate)) {
      requestError = 'Set minimum ≤ initial/fixed target ≤ maximum.'; ui.get('advanced').open = true; render(); return;
    }
    posting = true; requestError = ''; render();
    try {
      const result = await api.request('settings', {revision, settings});
      if (!alive) return;
      dirty = false; current = result.settings;
      if (!current.applying) fill();
      await api.refresh();
    } catch (error) { requestError = error.message; }
    finally { posting = false; render(); }
  });
  ui.cleanup(() => { alive = false; }); ui.cleanup(api.acquire());
  ui.cleanup(api.subscribe(({host, config}) => {
    const state = host?.settings || config?.settings;
    if (state) {
      const finished = current?.applying && !state.applying, changed = current?.revision !== state.revision;
      current = state;
      if (revision === null || (!dirty && !posting && !state.applying && (changed || finished))) fill();
    }
    render();
  }));
  if (control) ui.cleanup(control.subscribe(render));
  return {destroy: () => ui.destroy()};
}

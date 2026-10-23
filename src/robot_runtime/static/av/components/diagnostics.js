import {scope} from '../core.js';

export function mountDiagnostics(root, {api, media, control, networkTests = false} = {}) {
  const ui = scope(root, `<section class="robot-widget"><details><summary>Measurements and diagnostics</summary>
    <div class="robot-actions"><button data-ref="export">Save measurements</button><button data-ref="measure">Run network comparison · 110 s</button>
    <label data-ref="shape-label">Test link<select data-ref="shape"><option value="clear">Unlimited</option><option value="limited">Bandwidth limit</option>
    <option value="loss">1% packet loss</option></select></label></div><p class="robot-small">Timestamp estimates include source-reported frame age. They are not optical sensor-to-screen measurements.</p>
    <p class="robot-error" data-ref="error" role="alert"></p><pre data-ref="details"></pre></details></section>`);
  let alive = true;
  ui.get('measure').hidden = ui.get('shape-label').hidden = !networkTests;
  const render = () => {
    if (!alive) return;
    const state = api.state;
    ui.get('measure').disabled = !state.online || !state.config?.relay || state.host?.measuring || state.host?.settings?.applying || !media?.state.report?.frames_decoded;
    ui.get('shape').disabled = !state.online || !state.config?.relay || state.host?.measuring;
    if (ui.get('details').parentElement.open) ui.get('details').textContent = JSON.stringify({media: media?.state, control: control?.state, host: state.host}, null, 2);
  };
  const post = async (path, value) => { try { await api.request(path, value); } catch (error) { if (alive) ui.get('error').textContent = error.message; } };
  ui.on(ui.get('measure'), 'click', () => post('measure', {}));
  ui.on(ui.get('shape'), 'change', () => post('shape', ui.get('shape').value === 'limited' ? {limit_bps: api.state.config.test_bitrate} :
    ui.get('shape').value === 'loss' ? {loss_probability: .01} : {}));
  ui.on(ui.get('export'), 'click', () => {
    const value = {...(media?.exportData() || {last_host: api.state.host}), control: control?.state};
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = `${api.state.config?.name || 'diagnostics'}-browser.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  ui.on(ui.get('details').parentElement, 'toggle', render);
  ui.cleanup(() => { alive = false; }); ui.cleanup(api.acquire()); ui.cleanup(api.subscribe(render));
  if (media) ui.cleanup(media.subscribe(render));
  if (control) ui.cleanup(control.subscribe(render));
  return {destroy: () => ui.destroy()};
}

import {scope} from '../core.js';

export function mountCameraPicker(root, {api, control, media, declared = false} = {}) {
  const ui = scope(root, `<section class="robot-widget robot-camera"><h2>Camera source</h2><div class="robot-actions">
    <label><span data-ref="label">Camera</span><select data-ref="device" disabled><option>Waiting for Jetson</option></select></label>
    <button data-ref="refresh" disabled>Refresh cameras</button></div><p class="robot-small" data-ref="discovery" role="status"></p>
    <p data-ref="state" role="status"></p><p class="robot-error" data-ref="error" role="alert"></p></section>`);
  let signature = '', changing = false, refreshing = false, alive = true;
  if (declared) {
    root.querySelector('h2').textContent = 'Video source';
    ui.get('refresh').hidden = true;
    ui.get('label').textContent = 'Show';
  }
  const render = () => {
    if (!alive) return;
    const {online, host, config} = api.state, camera = host?.camera, settings = host?.settings;
    const selection = config?.source_selection;
    if (declared) root.hidden = !selection;
    if (!online || !camera) {
      ui.get('device').disabled = ui.get('refresh').disabled = true;
      ui.get('state').textContent = 'Camera status unavailable · check the Jetson connection'; return;
    }
    const devices = declared ? (selection?.choices || []) : [...camera.devices];
    const selected = declared ? selection?.id : camera.selected;
    if (!declared && selected && !devices.some(d => d.id === selected)) devices.push({id: selected, label: `${selected} · disconnected`});
    const next = JSON.stringify(devices.map(d => [d.id, d.label, d.available]));
    if (next !== signature) { signature = next; ui.get('device').replaceChildren(...devices.map(d => {
      const option = new Option(d.label + (d.available === false ? ' · not selected' : ''), d.id);
      option.disabled = d.available === false; return option;
    })); }
    if (!changing) ui.get('device').value = selected || '';
    ui.get('device').disabled = changing || settings?.applying || (!declared && settings?.controls_paused === false) || Boolean(control?.state.enabled);
    const pending = refreshing || camera.discovery.refreshing;
    ui.get('refresh').disabled = pending; ui.get('refresh').textContent = pending ? 'Refreshing…' : 'Refresh cameras';
    ui.get('discovery').textContent = pending ? 'Scanning for cameras…' : camera.discovery.error ?
      `Refresh failed: ${camera.discovery.error}. Previous list retained; refresh to retry.` :
      `Refresh after connecting another camera. ${camera.warnings.join('; ')}`;
    ui.get('state').textContent = camera.state === 'idle' ? 'Camera released · connect video to start preview' :
      camera.state === 'streaming' ? `${camera.output.width} × ${camera.output.height} · up to ${camera.output.fps} FPS output` :
      `${camera.state} · ${camera.error || 'Waiting for the selected camera'}`;
    if (declared) {
      ui.get('discovery').textContent = 'Changes the shared video source. Source processing continues. Connect video again after changing.';
      ui.get('state').textContent = `Showing: ${devices.find(d => d.id === selected)?.label || 'Waiting for source'}`;
    }
  };
  ui.on(ui.get('device'), 'change', async () => {
    changing = true; ui.get('error').textContent = ''; render();
    const body = {id: ui.get('device').value};
    if (declared) { body.source_revision = api.state.config.source_selection.revision; media?.disconnect(); }
    try { await api.request('camera', body); await api.refresh(); }
    catch (error) { if (alive) ui.get('error').textContent = error.message; }
    finally { changing = false; render(); }
  });
  ui.on(ui.get('refresh'), 'click', async () => {
    refreshing = true; ui.get('error').textContent = ''; render();
    try { await api.request('cameras/refresh', {}); await api.refresh(); }
    catch (error) { if (alive) ui.get('error').textContent = error.message; }
    finally { refreshing = false; render(); }
  });
  ui.cleanup(() => { alive = false; });
  ui.cleanup(api.acquire()); ui.cleanup(api.subscribe(render));
  if (control) ui.cleanup(control.subscribe(render));
  return {destroy: () => ui.destroy()};
}

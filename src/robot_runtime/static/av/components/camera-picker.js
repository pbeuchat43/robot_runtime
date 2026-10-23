import {scope} from '../core.js';

export function mountCameraPicker(root, {api, control} = {}) {
  const ui = scope(root, `<section class="robot-widget robot-camera"><h2>Camera source</h2><div class="robot-actions">
    <label>Camera<select data-ref="device" disabled><option>Waiting for Jetson</option></select></label>
    <button data-ref="refresh" disabled>Refresh cameras</button></div><p class="robot-small" data-ref="discovery" role="status"></p>
    <p data-ref="state" role="status"></p><p class="robot-error" data-ref="error" role="alert"></p></section>`);
  let signature = '', changing = false, refreshing = false, alive = true;
  const render = () => {
    if (!alive) return;
    const {online, host} = api.state, camera = host?.camera, settings = host?.settings;
    if (!online || !camera) {
      ui.get('device').disabled = ui.get('refresh').disabled = true;
      ui.get('state').textContent = 'Camera status unavailable · check the Jetson connection'; return;
    }
    const devices = [...camera.devices];
    if (camera.selected && !devices.some(d => d.id === camera.selected)) devices.push({id: camera.selected, label: `${camera.selected} · disconnected`});
    const next = JSON.stringify(devices.map(d => [d.id, d.label]));
    if (next !== signature) { signature = next; ui.get('device').replaceChildren(...devices.map(d => new Option(d.label, d.id))); }
    if (!changing) ui.get('device').value = camera.selected || '';
    ui.get('device').disabled = changing || settings?.applying || settings?.controls_paused === false || Boolean(control?.state.enabled);
    const pending = refreshing || camera.discovery.refreshing;
    ui.get('refresh').disabled = pending; ui.get('refresh').textContent = pending ? 'Refreshing…' : 'Refresh cameras';
    ui.get('discovery').textContent = pending ? 'Scanning for cameras…' : camera.discovery.error ?
      `Refresh failed: ${camera.discovery.error}. Previous list retained; refresh to retry.` :
      `Refresh after connecting another camera. ${camera.warnings.join('; ')}`;
    ui.get('state').textContent = camera.state === 'idle' ? 'Camera released · connect video to start preview' :
      camera.state === 'streaming' ? `${camera.output.width} × ${camera.output.height} · up to ${camera.output.fps} FPS output` :
      `${camera.state} · ${camera.error || 'Waiting for the selected camera'}`;
  };
  ui.on(ui.get('device'), 'change', async () => {
    changing = true; ui.get('error').textContent = ''; render();
    try { await api.request('camera', {id: ui.get('device').value}); await api.refresh(); }
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

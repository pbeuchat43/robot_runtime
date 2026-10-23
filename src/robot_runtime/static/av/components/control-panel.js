import {scope, number} from '../core.js';

export function mountControl(root, {control, title = 'Jetson input receipt'} = {}) {
  const ui = scope(root, `<section class="robot-widget robot-control"><div class="robot-heading"><h2 data-ref="title"></h2>
    <span class="robot-tag">NO ACTUATORS</span></div><div class="robot-actions">
    <button data-ref="connect" class="robot-primary">Connect input</button><button data-ref="disconnect" disabled>Disconnect input</button>
    <span data-ref="status" role="status"></span></div>
    <label class="robot-check"><input type="checkbox" data-ref="enabled" disabled> Enable diagnostic input</label>
    <p class="robot-small" data-ref="reason" role="status"></p><p class="robot-error" data-ref="error" role="alert"></p>
    <div class="robot-receipt"><h3 data-ref="receipt">No acknowledgement yet</h3><div class="robot-metrics">
    <div><b data-ref="seq">—</b><span>Acknowledged sequence</span></div><div><b data-ref="rtt">—</b><span>Round trip</span></div>
    <div><b data-ref="age">—</b><span>Acknowledgement age</span></div></div><p data-ref="preview">Jetson preview inactive</p></div>
    <details><summary>Control diagnostics</summary><pre data-ref="details"></pre></details></section>`);
  ui.get('title').textContent = title;
  ui.on(ui.get('connect'), 'click', () => control.connect());
  ui.on(ui.get('disconnect'), 'click', () => control.disconnect());
  ui.on(ui.get('enabled'), 'change', () => ui.get('enabled').checked ? control.enable() : control.pause());
  ui.cleanup(control.acquire());
  let metricsAt = -Infinity;
  ui.cleanup(control.subscribe(state => {
    ui.get('connect').disabled = ['connecting', 'connected'].includes(state.status);
    ui.get('disconnect').disabled = state.status === 'disconnected';
    ui.text('status', state.status);
    ui.get('enabled').checked = state.enabled; ui.get('enabled').disabled = !state.canEnable;
    ui.text('reason', state.reason); ui.text('error', state.error || '');
    ui.text('receipt', state.receiptFresh ? `Jetson confirmed · ${state.ack.source === 'simulation' ? 'simulated input' : 'live input'}` :
      state.ack?.active ? 'Jetson receipt stale · preview inactive' : state.ack ? `Jetson input inactive · ${state.ack.reason}` : 'No acknowledgement yet');
    ui.get('receipt').classList.toggle('robot-live', state.receiptFresh);
    // Human-readable counters need not invalidate text on every packet and ACK.
    const now = performance.now();
    if (now - metricsAt >= 100 || !state.ack) {
      metricsAt = now;
      ui.text('seq', state.ack ? `#${state.ack.seq}` : '—');
      ui.text('rtt', `${number(state.rtt, 0)} ms`); ui.text('age', `${number(state.ackAge, 0)} ms`);
      if (ui.get('details').parentElement.open) ui.text('details', JSON.stringify(state, null, 2));
    }
    const preview = state.ack?.preview;
    ui.text('preview', state.receiptFresh && preview ?
      `Jetson saw steering ${Math.round(preview.steering * 100)}% · throttle ${Math.round(preview.throttle * 100)}% · brake ${Math.round(preview.brake * 100)}%` : 'Jetson preview inactive');
  }));
  return {destroy: () => ui.destroy()};
}

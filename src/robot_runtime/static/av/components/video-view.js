import {scope, number} from '../core.js';

export function mountVideo(root, {media, title = 'Robot view', allowCurrent = true} = {}) {
  const ui = scope(root, `<section class="robot-widget robot-video"><div class="robot-heading"><h2 data-ref="title"></h2>
    <span data-ref="profile"></span></div><div class="robot-actions"><button class="robot-primary" data-ref="connect">Connect video</button>
    <button data-ref="current">Join current profile</button><button data-ref="disconnect" disabled>Disconnect video</button>
    <span data-ref="status" role="status"></span></div><p class="robot-error" data-ref="error" role="alert"></p>
    <div class="robot-video-frame"><video data-ref="video" autoplay muted playsinline></video><span data-ref="stale">No live image</span></div>
    <div class="robot-metrics"><div><b data-ref="fps">—</b><span>Received FPS</span></div><div><b data-ref="resolution">—</b><span>Received resolution</span></div>
    <div><b data-ref="bitrate">—</b><span>Received Mbit/s</span></div><div><b data-ref="latency">—</b><span>Source → display estimate</span></div>
    <div><b data-ref="age">Unknown</b><span>Content age bound</span></div><div><b data-ref="transport">—</b><span>Verified media path</span></div></div>
    <p class="robot-small" data-ref="clock"></p></section>`);
  ui.get('title').textContent = title;
  ui.get('current').hidden = !allowCurrent;
  ui.on(ui.get('connect'), 'click', () => media.connect());
  ui.on(ui.get('current'), 'click', () => media.connect('current'));
  ui.on(ui.get('disconnect'), 'click', () => media.disconnect());
  ui.cleanup(media.attachVideo(ui.get('video')));
  ui.cleanup(media.acquire());
  ui.cleanup(media.subscribe(state => {
    const active = !['error', 'disconnected'].includes(state.status);
    ui.get('connect').disabled = ui.get('current').disabled = active;
    ui.get('disconnect').disabled = !active;
    ui.text('status', state.status); ui.text('error', state.error);
    ui.text('profile', `${state.profile} profile`);
    const report = state.report, freshness = state.freshness, stamped = media.config?.stamp;
    const live = stamped ? freshness.fresh : freshness.arriving;
    ui.get('stale').hidden = Boolean(live);
    ui.text('stale', freshness.reason);
    ui.text('fps', number(report?.fps));
    ui.text('resolution', report?.width ? `${report.width} × ${report.height}` : '—');
    ui.text('bitrate', number(report?.mbps, 2));
    const delays = [...(report?.latency_ms || [])].sort((a, b) => a - b);
    ui.text('latency', delays.length ? `${Math.round(delays[Math.floor(delays.length / 2)])} ms` : '—');
    ui.text('age', freshness.age === null ? 'Unknown' : `≤ ${Math.round(freshness.age)} ms`);
    ui.text('transport', report?.transport_path?.replaceAll('_', ' ') || '—');
    ui.text('clock', !active ? 'Connect video to inspect frame freshness.' : !stamped ? 'Observer profile: frame arrival is monitored; capture-to-display age is not measured.' :
      state.clock ? `Clock uncertainty approximately ±${number(state.clock.uncertainty)} ms, plus timestamp and display precision.` : 'Clock calibration pending');
  }));
  return {destroy: () => ui.destroy()};
}

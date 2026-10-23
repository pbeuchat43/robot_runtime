/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */

// Constructors and imports are inert. Relative API paths preserve a host's mount prefix.
export function apiBase(value = '/') {
  const base = new URL(value, globalThis.location?.href);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
    throw new TypeError('Use an HTTP(S) API base without credentials, query or fragment');
  }
  if (!base.pathname.endsWith('/')) base.pathname += '/';
  return base;
}

export function endpoint(base, path) {
  if (typeof path !== 'string' || !path || path.startsWith('/') || path.includes('\\')) {
    throw new TypeError('API paths must be relative to the configured base');
  }
  const url = new URL(path, base);
  if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname) || url.hash) {
    throw new TypeError('API path escapes the configured base');
  }
  return url;
}

export function streamSelection(channel, topics = []) {
  if (!['runtime', 'observations', 'resources'].includes(channel)) throw new TypeError('Unknown stream channel');
  if (!Array.isArray(topics) || topics.length > 48 || topics.some(id => typeof id !== 'string' || !id || id.length > 128)) {
    throw new TypeError('Select at most 48 observation IDs');
  }
  const selected = [...new Set(topics)].sort();
  if (channel === 'observations' ? !selected.length : selected.length) {
    throw new TypeError('Only the observations channel takes a nonempty topic selection');
  }
  return {channel, topics: selected};
}

const numeric = value => typeof value === 'number' && Number.isFinite(value);
const aged = (value, elapsed) => numeric(value) && value >= 0 ? value + elapsed : null;

// The local read model is independent of HTTP/SSE. Epoch nanoseconds and int64s
// remain strings; only relative age fields use JavaScript numbers.
export function snapshotAt(channel, source, elapsed, connected) {
  if (source === null) return {snapshot: null, expiresIn: null};
  if (!source || typeof source !== 'object' || Array.isArray(source) ||
      (source.api_version !== undefined && source.api_version !== 1)) throw new TypeError('Unsupported snapshot');
  let expiresIn = null;
  const expire = seconds => { expiresIn = expiresIn === null ? seconds : Math.min(expiresIn, seconds); };
  let snapshot;
  if (channel === 'observations') {
    if (!Array.isArray(source.topics)) throw new TypeError('Observation snapshot needs topics');
    // Catalogs use explicit HTTP reads. The legacy SSE adapter still receives
    // repeated catalogs, but these do not become the observation client's state.
    const {api_version, config_revision, boot_id, server_time_ms, enabled, available, generation, capability_revision, error} = source;
    snapshot = {api_version, config_revision, boot_id, server_time_ms, enabled, available, generation, capability_revision, error};
    snapshot.topics = source.topics.map(topic => {
      if (!topic || typeof topic.id !== 'string') throw new TypeError('Observation needs an ID');
      const value = {...topic};
      for (const key of ['age_seconds', 'acquisition_age_seconds', 'sample_age_seconds', 'progress_age_seconds']) {
        value[key] = aged(topic[key], elapsed);
      }
      const limit = topic.stale_after_seconds;
      value.fresh = Boolean(connected && available && topic.fresh === true && numeric(limit) && limit > 0 &&
        value.age_seconds !== null && value.age_seconds <= limit &&
        (topic.acquisition_age_seconds == null || (value.acquisition_age_seconds !== null && value.acquisition_age_seconds <= limit)) &&
        (topic.sample_age_seconds == null || (value.sample_age_seconds !== null && value.sample_age_seconds < limit)));
      if (value.fresh) {
        for (const key of ['age_seconds', 'acquisition_age_seconds', 'sample_age_seconds']) {
          if (value[key] !== null) expire(limit - value[key]);
        }
      } else if (value.status === 'current') value.status = connected ? 'stale' : 'disconnected';
      return value;
    });
  } else {
    if (channel === 'runtime' && !Array.isArray(source.units)) throw new TypeError('Runtime snapshot needs units');
    const limit = channel === 'runtime' ? 15 : source.stale_after_seconds;
    const age = aged(source.age_seconds, elapsed);
    const fresh = Boolean(connected && source.fresh === true && age !== null && numeric(limit) && limit > 0 && age <= limit);
    snapshot = {...source, age_seconds: age, fresh};
    if (fresh) expire(limit - age);
    if (channel === 'runtime') snapshot.units = source.units.map(unit => ({...unit, fresh: fresh && unit.fresh === true,
      observation_age_seconds: aged(unit.observation_age_seconds, elapsed),
      ownership_observation_age_seconds: aged(unit.ownership_observation_age_seconds, elapsed)}));
  }
  return {snapshot, expiresIn};
}

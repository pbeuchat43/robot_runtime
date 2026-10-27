/* SPDX-License-Identifier: MIT
 * Copyright (c) 2026 Paul N. Beuchat */
const element = (tag, text, className) => {
  const value = document.createElement(tag);
  if (text !== undefined) value.textContent = text;
  if (className) value.className = className;
  return value;
};

// Mounts own their root and listeners only. No fetching, connection acquisition,
// robot names, visibility policy or command authority is hidden inside a view.
export function mountRuntime(root, {runtime}) {
  const heading = element('h2', 'Runtime'), connection = element('p'), units = element('div', undefined, 'ros-grid');
  root.replaceChildren(heading, connection, units);
  const unsubscribe = runtime.subscribe(({snapshot, connection: status}) => {
    connection.textContent = snapshot?.fresh ? 'Current runtime observations' : `Runtime ${status} · observations unavailable`;
    units.replaceChildren(...(snapshot?.units || []).map(unit => {
      const card = element('article', undefined, 'ros-card');
      card.dataset.runtimeUnit = unit.unit_id;
      card.append(element('h3', unit.label || unit.unit_id),
        element('p', `${unit.lifecycle} · ${unit.ownership}`),
        element('small', unit.run_id ? `Run ${unit.run_id}` : 'No current run'));
      return card;
    }));
  });
  return {destroy() { unsubscribe(); root.replaceChildren(); }};
}

export function mountObservations(root, {observations, topics, title = 'Observations'}) {
  const cards = element('div', undefined, 'ros-grid'), subscriptions = [];
  root.replaceChildren(element('h2', title), cards);
  for (const spec of topics) {
    const card = element('article', undefined, 'ros-card'), badge = element('p'), data = element('pre');
    card.dataset.rosTopic = spec.id;
    card.append(element('h3', spec.label), badge, data);
    cards.append(card);
    subscriptions.push(observations.subscribe(({snapshot, connection}) => {
      const value = snapshot?.topics.find(topic => topic.id === spec.id);
      const inactive = value?.status === 'inactive';
      badge.className = `badge ${value?.fresh ? 'live' : inactive ? '' : 'warning'}`;
      badge.textContent = inactive ? 'Not selected in this run' : value?.fresh ? `Fresh · sequence ${value.sequence}` : value?.status || connection;
      data.textContent = inactive ? 'No processing requested for this selection.' : value?.data == null ? 'No sample' : JSON.stringify(value.data, null, 2);
    }, {topics: [spec.id]}));
  }
  if (!topics.length) cards.append(element('p', 'This profile has no matching observation capabilities.'));
  return {destroy() { subscriptions.forEach(unsubscribe => unsubscribe()); root.replaceChildren(); }};
}

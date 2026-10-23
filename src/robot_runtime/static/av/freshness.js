// Conservative content age; arrival alone is not a capture timestamp.
export function stampAge(now, offset, stamp) {
  let age = (now + offset - stamp) % 4294967296;
  if (age < 0) age += 4294967296;
  return age < 60000 ? age : null;
}

export function videoFreshness(now, lastFrame, content, clock, policy) {
  const arrivalAge = lastFrame === null ? null : Math.max(0, now - lastFrame);
  const stalled = arrivalAge === null || arrivalAge >= policy.video_stall_ms;
  const calibrated = clock && now - clock.at < policy.clock_valid_ms &&
    clock.uncertainty <= policy.clock_max_uncertainty_ms;
  const age = calibrated && content ? content.age + Math.max(0, now - content.at) + content.uncertainty : null;
  return {age, arrivalAge, fresh: !stalled && age !== null && age < policy.video_max_age_ms,
    reason: stalled ? 'Video stalled' : age === null ? 'Image age unknown' :
      age >= policy.video_max_age_ms ? 'Image too old' : 'Fresh image'};
}

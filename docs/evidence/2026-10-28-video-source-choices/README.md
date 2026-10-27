# Declared live video choices and launch-aware status

Runtime 0.1.0.dev21 adds optional ROS image choices sharing one source owner and
domain, plus per-topic launch applicability. The owner keeps running during a
video-source switch. All media subscribers are revoked, old capture data is
cleared, and explicit video reconnection is required. Selection revisions reject
stale switches and admission requests. Single-topic profiles remain supported.

The implementation commits are `1f16c1b` and `015c177`. The latter ensures that
retained inactive-model observations become disconnected when their observation
connection is lost. Archive/squash consolidation preserves the same code tree.

On reserved Jah, 147 runtime unit tests passed. The final installed wheel passed
18 packaged Chromium checks, including live-choice revision handling, inactive
options, explicit reconnect, and loss of the observation connection. Compilation,
100 installed source-file comparisons, CLI entry-point help and installed-symlink
checks passed. [The software report](software.json) records hashes and checks.
The Python implementation was unchanged after its unit run; the final browser
state change was covered by the packaged browser suite.

These fixture checks do not establish camera or physical display performance.
The robot_composition dual-viewer evidence records native D455/OAK checks,
exact-image comparisons, uninterrupted detector generations/counters, deployment
and its retained initial test failures separately. No vehicle control or combined
overlay renderer was added.

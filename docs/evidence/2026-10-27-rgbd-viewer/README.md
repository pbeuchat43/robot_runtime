# Selectable RGB-D viewer verification — 27 October 2026

The installed `robot-runtime` 0.1.0.dev20 package was verified on reserved Jah
(Ubuntu 24.04 arm64, Python 3.12, ROS Jazzy, systemd 255, headless Chromium).
Execution used isolated state, names, domains and ports, with `PYTHONPATH` unset.

All 142 unit tests passed. The packaged browser-client suite passed all 16 checks,
including independent source selectors, requested/current-run separation,
confirmed-stop restart sequencing, cancellation and uncertainty, and explicit
reconnect after hidden-tab polling had paused. Both wheel and sdist were built;
a fresh venv installed the wheel and its declared web extra from the local
wheelhouse. All four CLI entry points passed `--help`, and every packaged source
asset was compared byte-for-byte with the tested source, including notices and
the vendored WebRTC facade.

The new software checks cover fixed allowed launch combinations, default mode
resolution, persisted mode identity, read-only launch preparation, generic viewer
source controls, aspect-preserving ROS-image fitting, and phase-preserving frame
pacing. Existing broader initial-snapshot evidence remains applicable where code
and contracts are unchanged; its complete matrix was not repeated.

During live recovery, explicit reconnect initially used a cached prior source
identity after polling had paused. Connect now awaits a shared refresh before
requesting new media admission. A subsequent source change still revokes the
session; commands are not replayed. The added browser regression exercises this
sequence with a hidden document and an already-pending refresh. The hardware
campaign also exposed fixture issues: a click before readiness and Chromium's
headless freeze leaving presentation callbacks suspended. The final finite
slow-viewer fixture blocks the browser main thread for eight seconds, then tests
source loss and recovery separately. Failed attempts remain in the private
verification archive and are not counted as acceptance passes.

- [Browser checks and cleanup](browser-clients.json)
- [Installed package assets and artifact hashes](package-identity.json)
- [Tested source and fixture hashes](source-sha256.json)

The sibling `robot_composition` repository records the camera/model matrix,
exact overlay samples, latency/rates/resources and persistent deployment audit.
Those measurements use real cameras and TensorRT with a headless browser. They
do not establish physical display scanout, moving-scene quality, physical USB
unplug recovery, long-duration reliability or surveyed spatial accuracy.

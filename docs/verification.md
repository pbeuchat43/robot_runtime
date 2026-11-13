# Verification

The earlier selectable-source development push is recorded in
[RGB-D viewer verification, 27 October 2026](evidence/2026-10-27-rgbd-viewer/README.md).
It adds 142-test and packaged-browser evidence plus component integration in the
sibling `robot_deployment` repository (then named `robot_composition`). The initial standalone baseline below
remains historical evidence for unchanged behavior.

The shared CLI, prepared restart, maintenance guard and native dependency builder
are covered by the [3 November deployment verification](../../robot_deployment/docs/evidence/2026-11-03-deployment/README.md).
The [4 November everyday-installation record](../../robot_deployment/docs/evidence/2026-11-04-everyday/README.md)
adds a complete permanent build, both-camera checks and management restart with
application workloads left stopped on Jah. Those records identify the exact
tested sources and artifacts. Fresh-image/Nano qualification remains open;
broader operational robustness testing is deferred until more of the system is
complete. The initial-snapshot results below retain their original scope.

## Direct OAK retirement, 14 November 2026

Runtime `0.1.0.dev29`, implementation `33854e97c0b248e731ac86c57bef17c43a42257a`,
was checked in an isolated installed wheel on reserved Jah. All 25 focused
camera/input/image tests passed, including rejection of retired direct OAK
profiles, V4L2 ownership conflicts and ROS worker signal cleanup. All 105
installed package files matched source, the source distribution built, and the
four preferred console commands accepted `--help`.

A finite synthetic ROS publisher delivered three images through the actual
`RosCapture` child process in a private domain; the frame sizes/ages passed and
the child exited after graceful close. This checks the retained transport after
its neutral rename. It does not establish camera, browser or display performance;
those unchanged paths retain their earlier evidence. The GStreamer preparation
helper remains supported, with its unused Python DepthAI copy option removed.

## Initial snapshot verification

Verified on 24 October 2026 (Australia/Sydney), using isolated temporary directories on `jah`. The environment was Ubuntu 24.04 arm64, Python 3.12, systemd 255, ROS 2 Jazzy, and headless Chromium. All application, browser, and native checks used the installed `robot-runtime` `0.1.0.dev19` wheel with `PYTHONPATH` unset.

**135 unit tests passed.** Thirteen integration suites passed **157 checks** covering:

- Native systemd lifecycle, ownership, descendant cleanup, restart reconciliation, and CLI access.
- Shared browser clients and native ROS topics/services, including the reference UI, three-tab delivery, and resource demand.
- Fixed-choice ROS commands, atomic parameters, snapshot capture, and action acceptance/results/cancellation/recovery.
- Synthetic cone, lidar, and fiducial observations using this repository's local test interface package.
- Native synthetic video and diagnostic input, including authentication, admission, WebRTC channel agreement, expiry, and cleanup.

Visual inspection then identified a spaced-out legacy label in the controller artwork. Only that SVG text changed. The installed wheel was rebuilt and both the client suite and native video/input browser suite passed again: **29 additional checks**. The retained evidence therefore contains 15 successful integration reports and 186 check executions. The runtime implementation was unchanged by this final presentation edit.

The cleanup audit checked 45 candidate services, found no remaining candidate processes, and verified removal of temporary browser profiles. Existing installed workload identities and management-service invocations were unchanged.

## Evidence and identity

- [Summary and cleanup audit](evidence/initial-snapshot/summary.json)
- [Final tested source and fixture hashes](evidence/initial-snapshot/tested-source-sha256.json)
- [Final tested wheel and package identity](evidence/initial-snapshot/package-identity.json)
- [Initial tested source hashes](evidence/initial-snapshot/initial-tested-source-sha256.json) and [initial package identity](evidence/initial-snapshot/initial-package-identity.json), before the SVG label edit
- [Unit results](evidence/initial-snapshot/unit.log), [integration progress](evidence/initial-snapshot/progress.log), and [suite commands](evidence/initial-snapshot/verify.sh)
- [Retained initial unit failure](evidence/initial-snapshot/unit-first.log): fixture profile construction incorrectly required a built ROS overlay. Removing that native dependency from pure unit-profile construction restored all 135 tests; native suites separately built and exercised the local interfaces.

The package records identify the artifacts actually used for execution. Documentation and evidence were assembled afterwards, so source distributions built from the final repository may have different archive hashes. Runtime and fixture identities are bound by the source hash manifests; final packaging checks compare installed package bytes against these files. Dependency wheels and private host-preservation audits remain in local artifacts.

## Initial snapshot limits

These checks establish selected software behavior on the recorded host. They do not establish physical camera/controller/vehicle behavior, five remote browsers, loaded perception performance, boot/logout persistence, stable-release resource targets, or broader hardware support. No new physical hardware acceptance or deployment was performed. The optional external-bridge and independent IMU integration suites were not rerun for these changes; their reusable tests remain available, with IMU unit coverage included in the full unit suite.

Use the [verification runbook](../tests/README.md) for isolated reproduction. Third-party license files and the bundled library remain byte-identical to their supplied versions.

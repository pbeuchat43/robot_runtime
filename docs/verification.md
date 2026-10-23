# Initial snapshot verification

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

## Limits

These checks establish selected software behavior on the recorded host. They do not establish physical camera/controller/vehicle behavior, five remote browsers, loaded perception performance, boot/logout persistence, stable-release resource targets, or broader hardware support. No new physical hardware acceptance or deployment was performed. The optional external-bridge and independent IMU integration suites were not rerun for these changes; their reusable tests remain available, with IMU unit coverage included in the full unit suite.

Use the [verification runbook](../tests/README.md) for isolated reproduction. Third-party license files and the bundled library remain byte-identical to their supplied versions.

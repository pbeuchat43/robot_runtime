# robot_runtime

A standalone ROS 2 runtime and browser platform for research, teaching, and outreach, developed first for Ubuntu on NVIDIA Jetson. Its design is informed by experience with `dream_system`, which focused on classroom deployment, while targeting broader applicability and independent integration.

The development package provides explicit process start/stop and recovery, a standalone CLI, authenticated browser commands, declared ROS topics/services/actions, live parameter inspection and editing, bounded spatial and diagnostic observations, requested image snapshots, and on-demand resource monitoring. Optional video and diagnostic gamepad services share runtime authentication and lifecycle. Diagnostic input does not grant vehicle-control authority.

The package is `robot_runtime`; its distribution and commands use `robot-runtime`. The current version is `0.1.0.dev19`. This is a development release: supported-host checks and synthetic fixtures do not establish stable-release sizing, hardware support, or vehicle acceptance.

Start with the [operator guide](docs/runtime-operator-guide.md) for standalone installation, configuration, CLI, HTTPS access, and recovery. ROS providers are declared through the [ROS operator guide](docs/ros-operator-guide.md). The runtime has no dependency on a particular robot workspace or installer.

## Documentation

- [Repository charter](docs/repository-charter.md) and [integration contract](docs/integration-contract.md): scope, ownership, lifecycle, authentication, freshness, and provisional resource objectives.
- [Runtime foundation](docs/runtime-foundation-design.md) and [implementation boundaries](docs/usable-runtime-slice.md): supervision, durable operations, and shared command policy.
- [Research platform architecture](docs/research-platform-architecture.md): responsibilities of the six planned repositories and proposed camera/perception contracts.
- [ROS topics/services](docs/ros-topic-service-slice.md), [parameters](docs/ros-parameters-slice.md), and [actions](docs/ros-actions.md).
- [Spatial observations](docs/spatial-observations.md), [cone/diagnostic projections](docs/cone-observations.md), [snapshots](docs/snapshots.md), and [resource monitoring](docs/resource-monitoring.md).
- [Browser clients](docs/browser-client-contract.md), [multiplexed delivery](docs/modular-runtime-delivery.md), and [camera/gamepad integration](docs/camera-gamepad-integration.md).
- [Verification instructions](tests/README.md) and [current verification record](docs/verification.md).

## Development

The canonical repository is [pbeuchat43/robot_runtime](https://github.com/pbeuchat43/robot_runtime). `main` is the default verified integration baseline; `dev` is for ongoing development. Both begin at the initial project snapshot. GitHub hosting does not imply a configured CI pipeline.

Follow [AGENTS.md](AGENTS.md). Execution checks run on the reserved development host `jah` with isolated resources. Existing deployments are outside this repository's ordinary development scope. Numerical objectives remain provisional: discuss difficult targets before prolonged optimization or changing their values.

Licensed under the [MIT License](LICENSE). Copyright (c) 2026 Paul N. Beuchat. Bundled third-party files retain their original licenses and attribution in [the vendor notices](src/robot_runtime/static/av/vendor/NOTICE.txt).

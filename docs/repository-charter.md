# robot_runtime repository charter

This charter defines project responsibilities, release scope, and operating policies. Numerical objectives are provisional and follow the discussion policy below.

## Purpose

`robot_runtime` provides reusable runtime management and browser access for ROS 2 robots. Its first deployment target is Ubuntu on NVIDIA Jetson, for independently integrated robot systems. The broader purpose is a reusable platform for research, teaching, and outreach.

Runtime management and browser support belong in one repository because commands, authorization, state reporting, and their presentation evolve together. They retain internal boundaries: the runtime core can be operated through a CLI without a running web server, and websites consume shared services rather than implement their own robot control rules.

The canonical repository is [pbeuchat43/robot_runtime](https://github.com/pbeuchat43/robot_runtime), with `main` as the default integration branch and `dev` for development. The project uses the [MIT License](../LICENSE), copyright Paul N. Beuchat. It must be usable on a prepared ROS 2 machine without a particular workspace layout or robot provider.

## Ownership

| Owner | Responsibility |
| --- | --- |
| `robot_runtime` | Runtime lifecycle and observations; shared command logic and CLI; browser API, client support and reference UI; authentication and pairing; configurable ROS access; runtime configuration schemas and supported installation interfaces |
| `robot_composition` | Select compatible versions, profiles, launch topology, transforms, and integrated acceptance |
| `robot_installer` | Provision the host and invoke component installation, upgrade, and recovery interfaces |
| Robot component repositories | Drivers, perception, policy, vehicle behavior, firmware, ROS interfaces, parameter validation, and component-specific health/readiness definitions |
| Activity-specific websites | Experiences for particular demonstrations or outreach activities, using the shared platform interfaces and permissions |

Media coordination and future driving authority belong in `robot_runtime`; vehicle actuation and media processing remain behind explicit component interfaces. This ownership does not require one process per component or one repository per website.

## First stable release

The first stable release includes:

- Explicit runtime start, graceful stop, status and bounded live-log access, through a CLI and reference browser interface.
- Shared observations that distinguish process lifecycle, ownership, application readiness, health/progress, and observation freshness.
- Public viewing of the website and exposed robot data on the local network; commands unlocked through a shared robot credential or pairing, enforced on the robot.
- Configurable access to permitted ROS topics, services and actions, including small telemetry, application state and transition requests, and live parameter inspection/editing where the provider supports them.
- Small visualizations, including feeds of up to 20 cones, and user-requested image snapshots from permitted ROS image sources.
- Shared backend facilities for multiple lightweight or richer websites, with resource use proportional to requested features and rates.
- Local operation without Internet access after installation, including website assets, authentication/pairing, and ROS integration.
- A documented standalone installation/configuration interface and basic recovery using ordinary Linux tools.

The supported sizing target is **20 managed runtime units and five simultaneous browser connections**. A runtime unit may launch several processes. This is a platform management target, not a promise that arbitrary perception workloads, image requests, or future video streams fit within the same resources.

## Later extensions

Manual driving remains a later extension. Optional live video and diagnostic input are implemented; their presence does not establish driving authority or physical stop behavior.

Manual driving requires one explicitly claimed controller across all websites. Other browsers may observe, but cannot drive or take over while that lease remains valid. Loss of the connection or fresh commands expires authority and requests the vehicle's defined stop behavior on the robot. Reconnection requires an explicit new claim and re-arm. The vehicle/provider contract must define what stopping physically means.

Live video uses a dedicated media path such as GStreamer/WebRTC rather than continuous ROS image delivery through the general telemetry bridge. Supported media profiles and native receiver options require their own measured acceptance. See the [media contract](camera-gamepad-integration.md).

Remote Internet access to robots, fleet management, individual user accounts, interactive terminal attachment, and compatibility with other runtime implementations' wire/state formats are outside the first-release commitment. Ordinary recovery does not require a parallel Bash implementation of the platform.

## Operating policies

| Situation | Required behavior |
| --- | --- |
| Robot boot | Start management infrastructure; do not start ROS application workloads automatically. |
| Workload failure | Record and report failure; leave the workload down until explicitly restarted. |
| Management or web failure | Existing workloads continue. Management restarts and reconciles its view with the actual supervisor state. |
| Normal stop | Use the provider's graceful shutdown contract. Report remaining processes on timeout; do not silently escalate to an unconditional kill. |
| Software update | Use a maintenance window with all application workloads stopped; update management, then explicitly start workloads. |
| Recovery | Document Linux commands to inspect services/logs and stop gracefully. Repair the platform before starting new workloads. |
| Browser connection | Viewing does not start sensors, detectors, policies, or other ROS workloads. |
| Deployment ownership | One shared robot Linux account and one shared runtime per Jetson. Browser authorization is a separate concern. |

Future driving expiry must continue to protect its command path even while ordinary workloads survive management failure. A process being alive, or telemetry being recent, must never by itself authorize driving.

## Architecture direction

Use shared Python application logic for runtime policy and commands, with thin CLI and web adapters. Shell remains appropriate for environment setup, installation/bootstrap, and operator recovery; duplicate command policy in Bash is unnecessary.

The implementation uses systemd transient user services and cgroup ownership. Verify descendant tracking, graceful-stop behavior, management recovery, and overhead on the target host. Avoid whole-host process scanning in ordinary runtime observation.

Runtime operations, observations, ROS access, authentication, image work, and future media/driving have explicit logical interfaces. Heavy image or media work must not block lifecycle commands or future driving expiry. The declared ROS worker and browser clients share bounded observations; alternative transports must preserve the same policy and freshness semantics.

## Resource objectives

These are provisional engineering targets, not achieved results. Measure on `jah`, recording hardware and power mode, software versions, workload, rates, browser count, duration, and measurement boundaries.

If evidence suggests an explicit numerical target will be difficult to meet, surface that early with the measured result, conditions, likely causes and tradeoffs. Discuss with Paul whether to continue pursuing the value or revise it. Do not silently lower a target or spend an extended implementation cycle optimizing toward it without that discussion. A revision records the rationale and updated acceptance criterion.

| Scenario or measurement | Initial target |
| --- | --- |
| 20 harmless idle units, no browsers | Management and observation CPU below 2% of one core on average |
| Same units, five runtime-only browsers | CPU below 5% of one core on average |
| Management, observation and small-message bridging | Below 250 MiB combined RSS; bounded queues and no retained growth across reconnect cycles |
| Cached runtime read | p95 below 50 ms |
| Normal runtime state-change delivery to a browser | p95 below 500 ms |
| Later: five viewers of one shared 20-cone feed at 20 Hz plus a 50 Hz driver | Management and small-message bridging CPU below 10% of one core |
| Later: driving-command acceptance/acknowledgement on the LAN | p95 round trip below 100 ms; this excludes physical vehicle response |

Measure the cone-only case separately during the first release. ROS application computation and dedicated video processing are excluded from management budgets and reported separately; incremental supervisor work is included. Set snapshot/media budgets once resolution, encoding, rate and concurrency profiles are specified. Measure both feature activation cost and resource release after the last consumer leaves. These averages and p95 targets do not define a driving safety timeout.

## Development and release boundary

The development implementation includes supervision, shared commands, authenticated browser access, declared ROS interfaces, bounded snapshots, and optional media/input services. A stable release still requires documented host/browser support, installation/recovery acceptance, and evidence for its sizing and resource targets.

Integrations select a release and supply profiles through `robot_composition` and `robot_installer`. Changes to existing deployments require explicit maintenance scope, compatible configuration/state handling, and rollback. No existing deployment is implicitly migrated by development in this repository.

All execution tests run on `jah` using isolated resources. Synthetic tests do not establish hardware or vehicle behavior. Hardware validation needs a defined setup and scope. API, configuration, and persistent-state versions belong to `robot_runtime`; incompatible migrations must be explicit.

The [research architecture](research-platform-architecture.md) defines upcoming camera and perception boundaries. Exact component messages, provider backends, and measured process layouts remain engineering work within those contracts.

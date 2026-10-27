# Declared ROS topics and services

The declared ROS worker exposes configured topics and services through the shared runtime, CLI, and authenticated browser API.

## Transport selection

| Candidate | Fit for this slice | Cost and boundary |
| --- | --- | --- |
| ROS-native `rclpy` worker over private pipes | Selected: exact capabilities, primitive-field projection, run checks and durable commands share the existing manager | Requires a small typed codec and process protocol; no new network listener or browser ROS authority |
| Installed Foxglove bridge | Useful existing ROS visualization transport with topic/service filters and CDR messages | A command gateway, field projection, run binding and durable intent would still be required; its visualization protocol is more than this slice needs |
| rosbridge_suite | General topic/service browser protocol with throttling and queues | Requires installing another server and constraining its broad ROS command surface, plus the same runtime-policy gateway |

This is a capability/architecture comparison, not a measured performance ranking. The initial native integration target is ROS Jazzy. Optional external bridges retain their own configuration and access policy.

Primary references: [Jazzy rclpy Node APIs](https://docs.ros.org/en/jazzy/p/rclpy/rclpy.node.html), [Jazzy service client APIs](https://docs.ros.org/en/jazzy/p/rclpy/rclpy.client.html), [dynamic interface loading](https://github.com/ros2/rosidl_runtime_py/blob/jazzy/rosidl_runtime_py/utilities.py), [Foxglove bridge configuration](https://github.com/foxglove/foxglove-sdk/blob/main/ros/src/foxglove_bridge/README.md), and [ROS 2 rosbridge protocol](https://github.com/RobotWebTools/rosbridge_suite/blob/ros2/ROSBRIDGE_PROTOCOL.md). Installed Jazzy signatures were also inspected directly on `jah`.

## Implementation boundary

The manager remains the single policy and durable-history owner. A ROS-native child uses an explicitly selected interpreter, sourced setup scripts and domain, with localhost discovery and a cleared inherited environment. The web environment does not import ROS. The child has no network API; bounded private pipes carry declared demands and service requests. Failure makes ROS observations unavailable and pending commands uncertain; recovery never replays a service request.

The optional `ros` profile block has its own schema version. It declares exact topic/service types and names, expected node names, unit bindings and exposed primitive fields. Initial supported fields are booleans, bounded-size strings, finite floats and integers; int64/uint64 values use canonical decimal strings on the wire. Nested messages and arrays are explicitly rejected in this slice. Topic QoS, rate, staleness and byte limits are explicit. Service request fields are typed and complete; selected response fields and an optional boolean success field describe provider acceptance.

One ROS worker serves all adapters. A topic has one upstream subscription regardless of viewer count; no subscription exists without demand from a CLI watcher or visible browser view. Pause, hide and disconnect release delivery and eventually the subscription. ROS graph discovery is a fixed worker cost when ROS is configured; it starts no application workloads. Latest observations replace older ones in bounded queues. Raw CDR size is checked before deserialization, and projected JSON has a separate byte budget. These application bounds do not limit memory already used inside DDS for incoming data.

A topic may declare `active_modes`: one to eight unique installed launch IDs of
its owning unit. When a live owned run selects another mode, the topic reports
`inactive`, `fresh: false`, and no sample data; it has no upstream subscription.
The reference views display **Not selected in this run** instead of implying a
provider failure. Omitting this optional field preserves applicability to every
launch. Stopped, unowned or stale runs keep the existing unavailable semantics.
Profiles using this field require runtime 0.1.0.dev21 or later.

Samples carry a worker and subscription generation, sequence, monotonic receipt time, explicit age/freshness and middleware source timestamp when provided. Plain scalar messages have no acquisition timestamp or spatial frame. A connection heartbeat does not refresh a sample. Stops, run changes, ambiguous graph endpoints and worker loss invalidate applicable samples. Graph identity constrains routing; it does not authenticate an OS process or prove that a ROS endpoint belongs to a cgroup.

Services share durable request UUIDs with existing operations. They require the profile revision, expected run and current ROS worker generation. The manager re-audits ownership immediately before authorizing the worker's dispatch; the worker checks the declared graph target and request deadline. One service request may be pending at a time, with no waiting queue. A response is correlated to its request; an optional success field distinguishes acceptance from refusal. Timeouts do not cancel remote execution. An uncertain request blocks further service requests to that run until local acknowledgement or a new run; acknowledgement does not replay or rewrite the old outcome.

Interface loading and provider discovery have separate availability fields. The browser waits for the declared service provider and a live owned run; every dispatch repeats the checks. Discovery and operation changes refresh capability metadata in other open browsers, including local acknowledgement of an uncertain operation.

## Verification scope

Use an isolated local-only ROS domain and unique finite fixture nodes. Verify typed/lossless data, shared subscriptions, rate/size limits, stale/empty/unavailable distinctions, demand release, duplicate graph rejection, authenticated CLI/browser service success/refusal, run/generation rejection, timeout uncertainty, deduplication and manager/worker recovery without replay. Run the existing lifecycle/auth checks where changes affect them, then build/install the package and exercise the browser against the installed assets. Retain observed source identity, failures and cleanup. No additional tuning cycle or hardware acceptance is included.

The implemented configuration and commands are documented in the [ROS operator guide](ros-operator-guide.md).

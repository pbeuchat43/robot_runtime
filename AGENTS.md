# Repository guidance

Read `README.md`, `docs/repository-charter.md`, and `docs/integration-contract.md` before changing behavior. They define scope, ownership, and operating policies. `docs/research-platform-architecture.md` records the agreed component boundaries and proposed camera/perception work.

The package is `robot_runtime` (`robot-runtime` distribution and commands). The canonical repository is `https://github.com/pbeuchat43/robot_runtime`, configured as `origin`. `main` is the verified integration baseline; `dev` is for ongoing development.

- Keep this platform independently installable. Robot-specific deployment paths, providers, and configuration belong in profiles and integration repositories.
- Preserve concurrent work. Changes to other repositories or existing deployments require their own scope.
- Run execution tests on the Jetson reached through `ssh jah`. Local source editing, Git operations, and document inspection are allowed. Reserve/coordinate the host before testing.
- Isolate test files, namespaces, units, and ports. Preserve production workloads, configuration, hardware, power settings, and other experiments.
- Follow the charter's early-discussion policy when a numerical target proves difficult. Do not silently change targets or extend optimization indefinitely.
- Distinguish synthetic evidence, observed software behavior, and observed hardware behavior. Record exact software/configuration identity and limits.
- Share command policy between adapters. Keep requested operations, source state, observed state, and freshness distinct.
- Use proportionate verification. `tests/README.md` describes unit, native ROS, systemd, CLI, browser, packaging, and resource checks. Preserve valid evidence instead of repeating unrelated checks.
- New project material uses MIT, copyright Paul N. Beuchat. Preserve licensing and attribution of reused third-party material.

Routine implementation decisions do not require repeated approval. Apply the user's skill and workflow preferences; this file does not mandate additional plans, reviewer agents, or approval ceremonies.

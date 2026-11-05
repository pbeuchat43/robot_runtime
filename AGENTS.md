# Repository guidance

Read `README.md`, `docs/repository-charter.md`, and `docs/integration-contract.md`
before changing behavior. They define scope, ownership, and operating policies.
For shared ownership and system boundaries, read the current
[system architecture](docs/research-platform-architecture.md) in this checkout.
Sections labelled as original proposals are historical context, not claims
about current implementation.

The package and preferred commands use `robot_runtime`; previous hyphenated
console names remain compatibility aliases. Packaging normalizes distribution
names independently of executable spelling. The canonical repository is
`https://github.com/pbeuchat43/robot_runtime`, configured as `origin`.

- Keep this platform independently installable. Robot-specific deployment paths, providers, and configuration belong in profiles and integration repositories.
- Preserve concurrent work. Changes to other repositories or existing deployments require their own scope.
- Run execution checks only on reserved Jah (`ssh jah`), using isolated files,
  processes, ROS domains and ports as applicable. Local editing, Git operations
  and static inspection are allowed.
- Isolate test files, namespaces, units, and ports. Preserve production workloads, configuration, hardware, power settings, and other experiments.
- Follow the charter's early-discussion policy when a numerical target proves difficult. Do not silently change targets or extend optimization indefinitely.
- Record exact software/configuration identities and evidence limits. Synthetic
  or software checks do not establish observed hardware or display performance.
- Share command policy between adapters. Keep requested operations, source state, observed state, and freshness distinct.
- Use proportionate verification. `tests/README.md` describes unit, native ROS, systemd, CLI, browser, packaging, and resource checks. Preserve valid evidence instead of repeating unrelated checks.
- New project material uses MIT, copyright Paul N. Beuchat. Preserve licensing and attribution of reused third-party material.
- `main` is the verified baseline; `dev` is for development. Work on feature or
  maintenance branches from `dev`. Squash into `dev` after the user's finish
  confirmation. Promote `main` only with separate agreement on a coordinated
  milestone, then synchronize `dev`. Follow the user's global preferences for
  history retention and cleanup. Passing checks alone does not authorize promotion.
- Coordinate public interface/configuration changes with affected consumers.
  Update package/interface versions and `robot_deployment/sources.json` pins
  where required; record compatibility, rebuild requirements and tested revisions.
  Run focused checks on both sides of the changed contract within the authorized scope.

Routine implementation decisions do not require repeated approval. Apply the user's skill and workflow preferences; this file does not mandate additional plans, reviewer agents, or approval ceremonies.

# AGENTS.md

Working notes for coding agents in the RoboStack all-distributions repository.

- Every distribution lives in `distros/<distro>/` (only distro-specific data); shared
  data is in `shared/`, tools in `tools/`. See README.md for the layout.
- Run everything per distribution with `pixi run rs <distro> <task>`
  (`generate-recipes`, `build`, `build-one <pkg>`, `check-patches`, `check-deps`,
  `create-snapshot`, `render-pinning`, `sort`).
- Patches: `distros/<distro>/patch/ros-<distro>-<pkg>.patch` (or `ros2-<pkg>`); port a patch
  to another distribution only if its source is compatible.
- A package rebuild without a new build number: add it to `evict_cache` in
  `distros/<distro>/ci.yaml` for the PR, reset it after merging.
- Never edit `conda_build_config.yaml` by hand: change `shared/pinning/` or the
  distribution's `distro.yaml`, then `pixi run rs <distro> render-pinning` (CI checks it).
- A change outside `distros/` rebuilds every distribution in CI; keep such PRs focused.

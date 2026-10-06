# AGENTS.md

Working notes for coding agents in the RoboStack all-distributions repository.

- Every distribution lives in `distros/<distro>/` (only distro-specific data); shared
  data is in `shared/` (vinca.yaml, pkg_additional_info.yaml, dependencies.yaml,
  robostack.yaml, pinning, tests), tools in `tools/`. See README.md for the layout and
  how they combine. Never edit `distros/<d>/work/` (generated).
- A fix that applies to every distribution goes into `shared/`; a version-specific one
  into `distros/<d>/`.
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
- `pixi run rs check` must pass (CI runs it): every distribution assembles, generated
  files are up to date, YAML is sorted.
- Moving a distribution from its old repository: `tools/import_distro.py` (see README).

---
name: robostack-versions
description: Update ROS package versions (rosdistro snapshot) and conda-forge pins for RoboStack distributions, bump build numbers and the distro mutex, plan full rebuilds, and find or rebuild packages built against outdated pins. Use for version updates, pinning/migration changes, pin conflicts, or full rebuilds.
---

# Versions, pins and rebuilds

robostack-bot does the routine part: `update-rosdistro-snapshot` and
`update-conda-forge-pinning` (weekly, or on request) open PRs that already contain the
bumps below. Do it by hand when the bot fails or something extra is needed.

## What is pinned where

- **ROS package versions**: `distros/<d>/rosdistro_snapshot.yaml`, plus
  `rosdistro_additional_recipes.yaml` for packages outside rosdistro.
- **conda-forge pins**:
  - `shared/pinning/conda_forge.yaml` holds the pinning version and migrations for
    every distribution that doesn't set its own.
  - `shared/pinning/overrides.yaml` holds local pins.
  - `distros/<d>/distro.yaml` can set `conda_forge_pinning_version` together with
    `conda_forge_migrations` (its own pinning, e.g. until its next full rebuild), and
    `pinning_overrides` (replace shared pins by name).
  - `pixi run rs <d> render-pinning` writes the committed
    `distros/<d>/conda_build_config.yaml`.
- **The mutex** (`mutex_package` in `distros/<d>/vinca.yaml`): `ros2-distro-mutex`
  with `upper_bound: x.x`. Packages built with mutex 0.11 can't be installed with
  0.10 builds. Its `run_constraints` pin the ABI-relevant libraries for users'
  environments and must agree with the rendered pins.

## Update the snapshot

```bash
pixi run rs <d> update-snapshot     # = bot command; summary of the version bumps
# or only the file:
pixi run rs <d> create-snapshot
```

`update-snapshot` also performs the rebuild bump (next section).

## Update the conda-forge pinning

```bash
pixi run rs update-pinning          # = bot command, for all distributions that follow shared/
pixi run rs <d> render-pinning      # after editing shared/pinning or distro.yaml
pixi run rs <d> check-deps          # solve all non-ROS deps + mutex constraints, nothing built
```

`check-deps` reports:
- `PIN MISMATCH`: a mutex `run_constraints` entry contradicts the rendered pin. Fix
  `vinca.yaml` or the pinning; never `conda_build_config.yaml`.
- Per conflicting package: the recipes that need it, the clashing pin, the solver
  explanation and the conda-forge migration status. "no migration" means the feedstock
  only needs a rebuild upstream.

Exit code 1 means something conflicts. Status of migrations:
https://conda-forge.org/status/.

## Rebuild bump (full rebuild)

When the snapshot or the pins change, everything is rebuilt:

1. `build_number` + 1 in `distros/<d>/vinca.yaml`.
2. Mutex minor version + 1, e.g. `0.10.0` -> `0.11.0`. Nothing else hard-codes the
   mutex version.
3. Remove per-package `build_number` entries in `distros/<d>/pkg_additional_info.yaml`
   that are <= the new global number; they would pin the old builds. Keep higher ones
   only if intentional.
4. Update mutex `run_constraints` to the new pins, then run `check-deps`.
5. `pixi run rs check`.

The bot does 1–4. It only moves constraints of the form `<pkg> <version>.*`; ranges
and other constraints are left for you to check.

In the PR, CI uses its cache unless told otherwise. A full rebuild PR sets
`full_rebuild: true` in `distros/<d>/ci.yaml` (reset after merging).

## Rebuild a few packages

- Published already: `<ros_pkg>: {build_number: <global + 1>}` in
  `distros/<d>/pkg_additional_info.yaml`. Shared settings stay in
  `shared/pkg_additional_info.yaml`; the distribution's keys win.
- Only in the current PR: `evict_cache` in `distros/<d>/ci.yaml`.

## Packages built against outdated pins

```bash
pixi run rs <d> find-stale                                     # = bot find-stale-packages
pixi run rs <d> check-deps --stale                             # local artifacts
pixi run rs <d> check-deps --stale --mutex-only                # only mutex violations
pixi run rs <d> check-deps --stale --repodata https://conda.anaconda.org/<channel>
pixi run rs <d> check-deps --stale --delete                    # delete stale local artifacts
```

For published stale builds, the report prints the `pkg_additional_info.yaml`
build-number snippet, a `mutex_package: build_number:` bump (re-publishes the mutex
with new constraints without changing its version), and the `anaconda remove`
commands for the old files.

## Bump vinca or rattler-build

Both are pinned once for all distributions in `pixi.toml` (vinca as a git revision).
After a bump, run `pixi install`, `pixi run rs check`, and
`pixi run rs <d> generate-recipes` for a distribution or two, and compare the recipe
sets before and after.

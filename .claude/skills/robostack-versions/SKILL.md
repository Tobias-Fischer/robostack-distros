---
name: robostack-versions
description: Update ROS package versions (rosdistro snapshot) and conda-forge pins for RoboStack distributions, bump build numbers and the distro mutex, plan full rebuilds, and find or rebuild packages built against outdated pins. Use for version updates, pinning/migration changes, pin conflicts, or full rebuilds.
---

# Versions, pins and rebuilds

robostack-bot does the routine part: `update-rosdistro-snapshot` (daily, when a new
rosdistro sync is tagged) and
`update-conda-forge-pinning` (daily, when the pinning version or the usable migrations
change, or on request) open PRs that already contain the bumps below. Do it by hand when the bot fails or something extra is needed.

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
  0.10 builds. It has no `run_constraints` (`rs check` enforces it): library versions
  come from the packages' own dependencies. When an environment resolves to a broken
  combination, fix the metadata of the package that is wrong (a patch,
  `pkg_additional_info.yaml`, or a conda-forge repodata patch), never the mutex.

## Update the snapshot

```bash
pixi run rs rosdistro-syncs         # which distributions have a new sync (what the bot checks daily)
pixi run rs <d> update-snapshot     # = bot command: latest sync, version bumps, rebuild bump
# or only the file, for a given sync tag (any ros/rosdistro ref) or rosdistro master:
pixi run rs <d> create-snapshot --rosdistro-ref <d>/2026-10-05
pixi run rs <d> create-snapshot
```

- The ROS release team tags every sync from ros-testing to the main repositories in
  ros/rosdistro (`<distro>/<YYYY-MM-DD>`, announced on Discourse as "New Packages for
  ..."). Snapshotting the tag ships what users get from the official repositories;
  rosdistro master also contains releases that are still only in ros-testing.
- `distros/<d>/distro.yaml` records the sync as `rosdistro_sync`. `rosdistro_sync:
  manual` (rolling, which isn't tagged regularly) uses rosdistro master instead and
  isn't checked daily.
- `update-snapshot` also performs the rebuild bump (next section).

## Update the conda-forge pinning

```bash
pixi run rs <d> update-pinning      # = bot command: latest pinning + rebuild plan for <d>
pixi run rs update-pinning          # the same for every distribution
pixi run rs <d> render-pinning      # after editing shared/pinning or distro.yaml
pixi run rs <d> check-deps          # solve all non-ROS deps together, nothing built
```

`check-deps` reports:
- Per conflicting package: the recipes that need it, the clashing pin, the solver
  explanation and the conda-forge migration status. "no migration" means the feedstock
  only needs a rebuild upstream.

Exit code 1 means something conflicts. Status of migrations:
https://conda-forge.org/status/.

`check-deps` takes the requirements of every selected package from vinca (`--source
recipes` uses `recipes/` instead, which with skip-existing only holds unbuilt packages).

### What the pinning PR rebuilds

`update-pinning` moves the distribution's pinning (its own `distro.yaml` version, or
the shared one it follows) forward and compares the old and new
`conda_build_config.yaml` with `vinca-rebuild-plan`: packages with a changed pin among their build or
host requirements, plus everything that depends on them (host or run, transitively),
are rebuilt; the rest keep their published builds.

- **Partial rebuild** (the usual case): those packages get `build_number` = highest
  released build + 1 in `distros/<d>/pkg_additional_info.yaml`. The mutex stays as
  it is, so all other published packages stay installable.
- **Full rebuild** (more than half of the packages affected): the bump below.

The PR lists the packages and why, the migrations used / waiting for our dependencies'
feedstocks, and a linux-64 `check-deps` result; it is labelled `dependency-conflict`
when the new pins can't be installed together.

```bash
pixi run vinca-rebuild-plan --old <old cbc> --new distros/<d>/conda_build_config.yaml \
  --vinca-dir distros/<d>/work      # the plan by hand (after `rs <d> prepare`)
```

## Rebuild bump (full rebuild)

When the snapshot changes (or a pinning change affects most packages), everything is
rebuilt:

1. `build_number` in `distros/<d>/vinca.yaml` = the highest build number of the
   distribution's released packages + 1, so every package gets a new build.
2. Mutex minor version + 1, e.g. `0.10.0` -> `0.11.0`. Nothing else hard-codes the
   mutex version.
3. Remove all per-package `build_number` entries in `distros/<d>/pkg_additional_info.yaml`.
4. `pixi run rs <d> check-deps`, then `pixi run rs check`.

The bot does 1–3.

In the PR, CI uses its cache unless told otherwise. A full rebuild PR sets
`full_rebuild: true` in `distros/<d>/ci.yaml` (reset after merging).

## Rebuild a few packages

- Published already: `<ros_pkg>: {build_number: <global + 1>}` in
  `distros/<d>/pkg_additional_info.yaml`. Shared settings stay in
  `shared/pkg_additional_info.yaml`; the distribution's keys win.
- Only in the current PR: `evict_cache` in `distros/<d>/ci.yaml`.

## ABI compatibility of ROS package updates

`tools/abi_check.py` compares builds of packages with libabigail's `abidiff`
(Linux only; run it in a Linux VM or CI):

```bash
pixi run abi-check --distro jazzy --platform linux-64 --latest-pairs 30   # pairs on the release channel
pixi run abi-check --old old.conda --new new.conda
pixi run abi-check --distro jazzy --pr-builds distros/jazzy/work/output/linux-64
```

Per package: `compatible`, `additions` (only added symbols), `soname` (SONAME
changed, symbols unchanged; dependents must be relinked, e.g. MoveIt's versioned
SONAMEs), `incompatible` (removed or changed symbols, or a removed library), or
`no libraries`. Published packages are stripped, so abidiff compares exported
symbols only; the installed headers (`include/`, ignoring comments and whitespace)
are compared too: unchanged headers rule out layout and inline changes, changed ones
are listed for review.

Every pull request runs `--pr-builds` in its linux-64 builds (non-blocking) and
posts one "ABI check" comment, updated on each push. Use it to accept pull requests
that bump single packages: `compatible`/`additions` can be bumped alone; for
`soname`/`incompatible` the comment lists the released dependents whose binaries
link to the changed library (DT_NEEDED; above 40 dependents they are listed
unchecked), whose build numbers (`pkg_additional_info.yaml`; the comment has the entries to paste, with the next free build number) must be bumped in the
same pull request. A changed non-ROS pin listed in the comment is part of the
comparison.

## Packages built against outdated pins

```bash
pixi run rs <d> find-stale                                     # = bot find-stale-packages
pixi run rs <d> check-deps --stale                             # local artifacts
pixi run rs <d> check-deps --stale --repodata https://conda.anaconda.org/<channel>
pixi run rs <d> check-deps --stale --delete                    # delete stale local artifacts
```

For published stale builds, the report prints the `pkg_additional_info.yaml`
build-number snippet and the `anaconda remove` commands for the old files.

## Bump vinca or rattler-build

Both are pinned once for all distributions in `pixi.toml` (vinca as a git revision).
After a bump, run `pixi install`, `pixi run rs check`, and
`pixi run rs <d> generate-recipes` for a distribution or two, and compare the recipe
sets before and after.

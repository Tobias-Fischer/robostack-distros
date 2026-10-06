---
name: robostack-patch
description: Create, refresh, place, port and validate source patches for RoboStack ROS packages (distros/<d>/patch/). Use after editing sources in a rattler-build work directory, when a patch no longer applies, or when porting a fix to another distribution.
---

# Patches

## Where they live

- `distros/<d>/patch/ros-<d>-<pkg>.patch` (all platforms), or with a platform suffix
  `.osx.patch`, `.linux.patch`, `.win.patch`, `.unix.patch`. `<pkg>` uses dashes.
- One patch per package and platform. Merge new hunks into the existing file instead
  of adding variants.
- vinca picks them up by name when generating recipes; nothing else needs wiring.
  `pixi run rs check` rejects names of other distributions.
- `distros/<d>/patch/dependencies.yaml` is for dependency fixes, not source patches
  (`shared/dependencies.yaml` for all distributions).

## Create a patch from work-directory edits

```bash
WORK=$(ls -td distros/<d>/work/output/bld/rattler-build_ros2-<pkg>_*/work | head -1)
cd "$WORK"
# edit the sources, rebuild with `bash conda_build.sh` (after `source build_env.sh`)
rattler-build create-patch --directory . --name ros-<d>-<pkg> \
  --exclude "*.o,*.so,*.dylib,*.a,*.pyc,__pycache__,build/" --dry-run
rattler-build create-patch --directory . --name ros-<d>-<pkg> \
  --exclude "*.o,*.so,*.dylib,*.a,*.pyc,__pycache__,build/"
```

Move the result to `distros/<d>/patch/ros-<d>-<pkg>.patch`, merging it with an
existing patch for that package if there is one. Then:

```bash
pixi run rs <d> build-one ros2-<pkg>
```

Without a work directory, a `git diff` against the source checkout from
`.source_info.json` works too (paths relative to the source root, `a/` and `b/` prefixes).

## Check that patches apply

```bash
pixi run rs <d> check-patches                          # every recipe with patches
pixi run rs <d> check-patches --recipe ros2-<pkg>      # one (repeat --recipe for more)
pixi run rs <d> check-patches --dry --recipe ros2-<pkg>  # prepare only
pixi run rs <d> check-orphaned-patches                 # platform patches without a recipe
```

## Refresh a patch that no longer applies

A new upstream version moved the code: build the package without the patch (rename it
temporarily), re-apply the change by hand in the work directory, regenerate with
`create-patch`, and check that the old intent is still needed. Upstream may have fixed
it, in which case delete the patch.

## Port a fix to another distribution

- Port only to an existing package with a compatible source. Compare the versions in
  `distros/<d>/rosdistro_snapshot.yaml` and check with `check-patches`. Never copy a
  patch only because a file of that name exists elsewhere.
- Rename to the target distribution (`ros-<other>-<pkg>.patch`).
- After the PR's first build, add the package to `evict_cache` in the target's
  `ci.yaml` if CI already cached it.
- Fixes that belong upstream: open a PR on the package's repository and note it in the
  patch header.

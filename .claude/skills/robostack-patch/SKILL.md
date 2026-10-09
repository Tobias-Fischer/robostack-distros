---
name: robostack-patch
description: Create, refresh, place, port and validate source patches for RoboStack ROS packages (distros/<d>/patch/). Use after editing sources in a rattler-build work directory, when a patch no longer applies, or when porting a fix to another distribution.
---

# Patches

## Where they live

- `distros/<d>/patch/ros2-<pkg>.patch` (all platforms), or with a platform suffix
  `.osx.patch`, `.linux.patch`, `.win.patch`, `.unix.patch`. `<pkg>` uses dashes.
- One patch per package and platform. Merge new hunks into the existing file instead
  of adding variants.
- vinca picks them up by name when generating recipes; nothing else needs wiring.
  `pixi run rs check` rejects other names.
- `distros/<d>/patch/dependencies.yaml` is for dependency fixes, not source patches
  (`shared/patch/dependencies.yaml` for all distributions).
- Shared patches live in `shared/patch/`. A distribution override replaces the
  package's entire shared patch set, including its platform variants.

## Create a patch from work-directory edits

```bash
WORK=$(ls -td distros/<d>/work/output/bld/rattler-build_ros2-<pkg>_*/work | head -1)
cd "$WORK"
# edit the sources, rebuild with `bash conda_build.sh` (after `source build_env.sh`)
rattler-build create-patch --directory . --name ros2-<pkg> \
  --exclude "*.o,*.so,*.dylib,*.a,*.pyc,__pycache__,build/" --dry-run
rattler-build create-patch --directory . --name ros2-<pkg> \
  --exclude "*.o,*.so,*.dylib,*.a,*.pyc,__pycache__,build/"
```

Move the result to `distros/<d>/patch/ros2-<pkg>.patch`, merging it with an
existing patch for that package if there is one. Then:

```bash
pixi run rs <d> generate-recipes
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

Both checks resolve shared/distribution inheritance and platform selectors in the
same way as recipe generation. Clean-apply checks cover the distinct patch sets
for every target platform, regardless of the host.

## Refresh a patch that no longer applies

A new upstream version moved the code: build the package without the patch (rename it
temporarily), re-apply the change by hand in the work directory, regenerate with
`create-patch`, and check that the old intent is still needed. Upstream may have fixed
it, in which case delete the patch.

## Port a fix to another distribution

- Port only to an existing package with a compatible source. Compare the versions in
  `distros/<d>/rosdistro_snapshot.yaml` and check with `check-patches`. Never copy a
  patch only because a file of that name exists elsewhere.
- The file name stays the same in every distribution (`ros2-<pkg>.patch`), so
  `diff distros/{jazzy,humble}/patch/ros2-<pkg>.patch` shows how two distributions differ.
- Changed patches automatically start a fresh PR cache epoch. `evict_cache` in the
  target's `ci.yaml` requests one explicitly, invalidating the whole
  distribution/platform cache, including dependents. Retries keep replacement builds.
- Fixes that belong upstream: open a PR on the package's repository and note it in the
  patch header.

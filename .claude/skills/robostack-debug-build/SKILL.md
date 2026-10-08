---
name: robostack-debug-build
description: Debug and fix a RoboStack package that fails to build (compile, link, configure, patch or test failures) for one ROS distribution. Use when a build-one / CI build of a ros2-<pkg> recipe fails.
---

# Debug a failed package build

Work from the repository root; `<d>` is the distribution, `<pkg>` the conda name
(`ros2-<ros-name-with-dashes>`).

## 1. Reproduce with one package

```bash
pixi run rs <d> generate-recipes          # if distros/<d>/work/recipes is missing or stale
pixi run rs <d> build-one ros2-<pkg>
```

`build-one` copies the package's patches into the recipe and builds without
`--skip-existing`. For a CI failure, first read the failing job's log
(`gh run view <run> --job <job> --log-failed`) and search for `× error`,
`CMake Error`, `error:` and `undefined reference`.

## 2. Find the work directory

```bash
ls -td distros/<d>/work/output/bld/rattler-build_ros2-<pkg>_*/work | head -1
```

In it:
- `conda_build.log`: the full log. Look for compile, link and configure errors,
  patch failures, missing files.
- `build_env.sh`: `PREFIX` (host env), `BUILD_PREFIX`, `SRC_DIR`, `RECIPE_DIR`.
- `.source_info.json` (`jq .`): the fetched source revision.

## 3. Investigate by failure class

| symptom | look at |
|---|---|
| missing header | `requirements.host` of the recipe; is it under `$PREFIX/include`? |
| `Could not find a package configuration file provided by "X"` | is X in host? Does a conda-forge package provide that CMake config? A mapping in `shared/robostack.yaml` may be wrong, or vinca may have built a ROS package under the conda-forge name (see below) |
| undefined symbols | host deps, `$PREFIX/lib`, link flags |
| configure failure | flags in `conda_build.sh`; rerun with verbosity |
| patch does not apply | refresh it, see the `robostack-patch` skill |
| hard-coded prefixes / rpaths | relocatability; inspect with `otool -L` (macOS) / `patchelf --print-rpath` |

Reproduce interactively:

```bash
cd <work-directory>
source build_env.sh
bash -x conda_build.sh 2>&1 | less
```

## 4. Common fixes

- **Boost 1.88+**: needs C++14 or newer; replace `-std=c++11` / `CMAKE_CXX_STANDARD 11`.
- **Python modules on macOS**: don't link `Python::Python`:
  ```cmake
  if(APPLE)
    set_target_properties(${_name} PROPERTIES LINK_FLAGS "-undefined dynamic_lookup")
  else()
    target_link_libraries(${_name} ${PYTHON_LIBRARIES})
  endif()
  ```
- **Windows `min`/`max` macro clashes**: `target_compile_definitions(<target> PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)` under `if(WIN32)`.
- **gtest / test failures**: add the missing dependency in `dependencies.yaml`, or
  disable the tests when safe; no custom shims.
- **Qt plugins (rviz, rtabmap)**: make sure CMake finds the intended Qt major version.
- **Missing dependency**: `shared/dependencies.yaml` (all distributions) or
  `distros/<d>/patch/dependencies.yaml` (one), keyed by ROS package name:
  ```yaml
  <ros_pkg>:
    add_host: [<conda-pkg>]
    add_run: [<conda-pkg>]
  ```
- **A conda-forge package is shadowed**: when a rosdep key that is also a ROS package
  maps to a conda-forge package (e.g. `tl_expected` -> `cpp-expected`), vinca may build
  the ROS package under the conda-forge name. Add it to `packages_skip_by_deps`
  (`shared/vinca.yaml`), and evict the stale build from the PR cache
  (`evict_cache` in `distros/<d>/ci.yaml`, plain names work).
- **CMake args for one package**: `additional_cmake_args` in `pkg_additional_info.yaml`.

## 5. Make it stick

1. Turn source edits into a patch (`robostack-patch` skill).
2. Rebuild: `pixi run rs <d> build-one ros2-<pkg>`.
3. If the fixed package was already built in the PR, add it to `evict_cache` in
   `distros/<d>/ci.yaml`. A package that is already published needs a new build
   number: `<ros_pkg>: {build_number: <global + 1>}` in `distros/<d>/pkg_additional_info.yaml`.
4. Check whether the same fix applies to other distributions; port it only where the
   source is compatible.

Triage order with many failures: fix the easy ones first, move on once something gets
complex, and come back after the queue is shorter.

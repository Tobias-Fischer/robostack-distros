---
name: robostack-package-selection
description: Add, exclude or restrict ROS packages in RoboStack vinca.yaml files (shared/ selection, packages_exclude / packages_skip per distribution), including platform-only packages and the build gap report. Use when a package should be added, removed, made Linux-only, or when recipes/artifacts don't line up.
---

# Package selection (vinca.yaml)

`distros/<d>/work/vinca.yaml` is assembled from `shared/vinca.yaml` plus
`distros/<d>/vinca.yaml` (`tools/robostack.py`, `merge_vinca`): the package lists are
combined, vinca applies them.

## Selecting packages

All selections are in **`shared/vinca.yaml`** (`packages_select_by_deps`), with the
broadest platform condition that works:

```yaml
packages_select_by_deps:
  - some_package
  - if: linux
    then:
      - some_linux_only_package
```

vinca skips a selected package that a distribution's rosdistro doesn't have ("not in
available packages anymore"), so a package that only some distributions have is
still selected in `shared/`. Add packages with the bot or:

```bash
pixi run rs add-package foxglove_bridge rqt_gauges            # lists what each distribution builds
pixi run rs add-package foxglove_bridge humble jazzy --preview
```

## Exceptions per distribution (or for all, in shared/)

Both keys take plain names (everywhere) or `- if: <condition>` / `then:` blocks (on
those platforms only), and both drop the package from the shared selection there:

- **`packages_exclude`**: never built, and dropped from the dependencies of every other
  package (vinca's `packages_skip_by_deps` + `packages_remove_from_deps`). The usual
  choice: a package that doesn't build on a platform, and nothing needs it.
- **`packages_skip`**: never built, but dependents keep depending on it (vinca's
  `packages_skip_by_deps` only), e.g. when an already published build must be used
  (rolling's `rviz_visual_tools`, which `moveit_visual_tools` needs).

ROS packages whose name `shared/robostack.yaml` maps to a conda-forge package (e.g.
`tl_expected` -> `cpp-expected`) are skipped automatically, so the conda-forge package
is used and not shadowed by a ROS build of the same name.

vinca rejects the old `packages_skip_by_deps` / `packages_remove_from_deps` keys. vinca's revision is pinned in `pixi.toml`; if that pin moves, re-check
`vinca/main.py` and `vinca/resolve.py`.

## Recipes vs. artifacts

```bash
pixi run rs <d> generate-recipes --platform <platform>
pixi run rs <d> gap-report
```

- **Built artifacts without a recipe directory**: packages in
  `distros/<d>/work/output/<platform>` that aren't selected any more. Add their seeds,
  or accept that they are gone.
- **Recipe directories without an artifact**: generated recipes still to build on this
  platform.

Regenerate recipes after editing `vinca.yaml` before expecting the report to change.

## rosdep mappings

`shared/robostack.yaml` maps rosdep keys to conda packages (per platform);
`robostack: []` drops a key. Changes there affect every
distribution: check them with `generate-recipes` for at least two distributions.

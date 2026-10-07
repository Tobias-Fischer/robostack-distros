---
name: robostack-package-selection
description: Add, exclude or restrict ROS packages in RoboStack vinca.yaml files (shared/ and distros/<d>/), including platform-only packages, dependency skips and the build gap report. Use when a package should be added, removed, made Linux-only, or when recipes/artifacts don't line up.
---

# Package selection (vinca.yaml)

`distros/<d>/work/vinca.yaml` is assembled from `shared/vinca.yaml` plus
`distros/<d>/vinca.yaml`:

- **All package selections are in `shared/vinca.yaml`** (`packages_select_by_deps`),
  with the broadest platform condition that works. vinca skips a selected package that
  a distribution's rosdistro doesn't have ("not in available packages anymore"), so
  a package only some distributions have is still selected in `shared/`.
- **A distribution lists only exceptions**, in `packages_deselect`: a plain name drops
  the shared selection everywhere, a name under `- if: <condition>` / `then:` drops it
  on those platforms only (e.g. a package that fails on Windows in humble only).
  Deselecting doesn't stop the package from being pulled in as a dependency; add it to
  `packages_skip_by_deps` too for that.
- `packages_skip_by_deps` / `packages_remove_from_deps` are concatenated from both
  files; for other keys the distribution wins.

## Add packages

```bash
pixi run rs add-package foxglove_bridge rqt_gauges            # every distribution that has them
pixi run rs add-package foxglove_bridge humble jazzy --preview
```

This is the bot's `add-package`. It inserts in sorted position, chooses `shared/` when
every distribution gets the same packages, and lists what will be built on linux-64.
Names are ROS package names; dashes and underscores both work.

## The three ways to exclude a package

vinca's revision is pinned in `pixi.toml`. If that pin moves, re-check
`vinca/main.py` and `vinca/resolve.py`.

1. **`packages_select_by_deps` under a platform condition**: the primary way to keep a
   package's own recipe off a platform. Every listed name is selected
   unconditionally, so not listing it for a platform is what excludes it:
   ```yaml
   packages_select_by_deps:
     - if: linux
       then:
         - some_linux_only_pkg
   ```
2. **`packages_skip_by_deps`**: only stops transitive pull-in (`ignore_pkgs` for
   `distro.get_depends()`). It doesn't stop a package listed directly in (1). It's
   also the tool for ROS packages that map to conda-forge in `shared/robostack.yaml`
   (`tl_expected`, `sophus`), so that vinca doesn't build them under the conda-forge
   name.
3. **`packages_remove_from_deps`**: removes the package's own recipe *and* strips it
   from every other package's host/run dependencies, together and inseparably. Use
   it only if nothing selected legitimately needs it; otherwise use (1) + (2).

Keep (2) and (3) coherent with the platform conditions in (1). A truly platform-only
package goes behind a condition, not into a comment.

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
`shared/packages_ignore.yaml` lists keys to drop. Changes there affect every
distribution: check them with `generate-recipes` for at least two distributions.

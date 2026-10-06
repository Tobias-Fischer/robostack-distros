# robostack-distros (prototype)

**Prototype:** one repository for all RoboStack ROS 2 distributions, instead of one
repository per distribution (RoboStack/ros-rolling, ros-humble, ros-jazzy,
ros-kilted, ros-lyrical) kept in sync through a template
([ros-distro-template](https://github.com/Tobias-Fischer/ros-distro-template)).
The data is copied from those repositories (rolling, humble, lyrical and kilted
`main`, jazzy's `codex/cross-distro-sync`); their git history is not imported.

## Layout

```
distros/<distro>/          everything specific to one distribution
  distro.yaml              channel, upload target, conda-forge pinning version/migrations, own pins
  vinca.yaml               package selection, build number, mutex
  pkg_additional_info.yaml, rosdistro_additional_recipes.yaml, rosdistro_snapshot.yaml
  patch/
  ci.yaml                  temporary PR-build controls (full rebuild, cache evictions)
  conda_build_config.yaml  generated: `pixi run rs <distro> render-pinning`
shared/                    the same for every distribution
  robostack.yaml, packages-ignore.yaml   rosdep key -> conda package mapping
  pinning/                 conda-forge pinning version, migrations and overrides
  tests/                   package tests (ros2-<pkg>.yaml; *.jinja for distro-specific bits)
tools/robostack.py         `pixi run rs <distro> <task>`: runs everything for one distribution
tools/*.py                 check_patches_clean_apply, check_dependency_compat, ...
.scripts/                  staged build scripts (build_unix.sh, build_win.bat)
.github/workflows/         testpr.yml (PR builds), main.yml (staged build branches)
pixi.toml                  one environment for all distributions (one vinca, one rattler-build)
```

## Working with it

```bash
pixi run rs jazzy generate-recipes            # vinca -m inside distros/jazzy
pixi run rs jazzy build                       # build everything missing on the channel
pixi run rs jazzy build-one ros2-ros-workspace
pixi run rs jazzy check-patches
pixi run rs jazzy check-deps
pixi run rs jazzy create-snapshot
pixi run rs jazzy render-pinning              # after editing shared/pinning or distro.yaml
pixi run sort                                 # all YAML files of all distributions
```

## CI

- **Pull requests** (`testpr.yml`): a `plan` job looks at the changed files.
  - A PR that only touches `distros/<d>/` builds just `<d>`.
  - A change anywhere else (shared files, tools, CI) builds every distribution.
  - Each selected distribution is built on all five platforms, with its own build cache.
  - A sort check also verifies that every `conda_build_config.yaml` matches `shared/pinning` and the distro's `distro.yaml`.
- **After a merge** (`main.yml`): for each changed distribution and platform, the job generates the recipes and the staged build workflow. The workflow is named after the distribution (`build_<distro>_<platform>.yml`) and runs `.scripts/build_*.sh` inside `distros/<distro>`. The job pushes it to `buildbranch_<distro>_<platform>`.
  - In this prototype, branches are only pushed in the RoboStack organisation. Elsewhere, the generated workflows are uploaded as an artifact.

## Compared with one repository per distribution + template

**What gets simpler**
- No template, copier, update PRs, drift checks or "upstream to template": shared files exist once, and a change to them is one PR, tested on every distribution.
- Cross-distribution changes (a patch for several distros, a mapping in `robostack.yaml`, a pinning bump) are one PR instead of five.
- One `pixi.toml`/`pixi.lock`, one vinca version, one place for issues and docs.

**What gets harder or needs a decision**
- **Issues and discoverability.** Users know `RoboStack/ros-humble`. Issues for all distros land in one tracker (labels per distro), and the per-distro README badges and links on robostack.github.io need updating. Old repos could be archived with a pointer.
- **CI load.** A shared change builds every distribution: 5 × 5 jobs. With `--skip-existing` most do nothing, but a pinning bump means rebuilding everything in one PR. In practice you'd bump one distribution at a time (the `conda_forge_pinning_version` in its `distro.yaml`) and the shared one last.
- **Staged builds.** There are 25 build branches in one repository, all sharing that repository's concurrency and Actions minutes. GitHub limits concurrency per account/organisation anyway, so this is no worse than today.
- **Release independence.** Distributions are still rebuilt independently: own build number, mutex, snapshot and pinning version. But `main` must stay green for all of them: a broken shared change blocks every distribution's merges.
- **History.** Importing the five histories (`git subtree`/`filter-repo` into `distros/<d>/`) is possible but noisy; starting fresh and archiving the old repositories is simpler.
- **Permissions.** Today maintainers can be scoped per repository. In one repository that needs CODEOWNERS per `distros/<d>/`.

## Status

**Works:**
- `rs` tasks.
- Shared conda index and tests (vinca reads `../../shared/*.yaml`; the tests are copied next to `vinca.yaml`).
- Rendered pinning.
- PR builds selecting the changed distributions.

lyrical's full linux-64 recipe set is identical to the template-based ros-lyrical.

**Not done:**
- importing history;
- the bot (most commands are simpler here: no template sync; `update-rosdistro-snapshot` and `find-stale-packages` would take a distribution argument);
- uploading from build branches.

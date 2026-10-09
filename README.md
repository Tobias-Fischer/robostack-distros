# RoboStack: ROS 2 distributions

Conda packages of [ROS](https://www.ros.org) for Linux, macOS and Windows, built from
this repository for every supported ROS 2 distribution. To install and use them, see
the [RoboStack documentation](https://robostack.github.io/GettingStarted.html).

| Distribution | Channel |
|---|---|
| rolling | [robostack-rolling](https://prefix.dev/channels/robostack-rolling) |
| lyrical | [robostack-lyrical](https://prefix.dev/channels/robostack-lyrical) |
| jazzy | [robostack-jazzy](https://anaconda.org/robostack-jazzy) |
| humble | [robostack-staging](https://anaconda.org/robostack-staging) |

Packages are named `ros2-<package>`, e.g. `ros2-desktop` or `ros2-rclcpp`; the
distribution comes from the channel. The older `ros-<distro>-<package>` names are no
longer built.

**A package is missing?** Open a *Package request* issue. robostack-bot opens a pull
request that adds it to the build, and a maintainer reviews it.

If you use RoboStack in your academic work, please cite:

```bibtex
@article{FischerRAM2021,
    title={A RoboStack Tutorial: Using the Robot Operating System Alongside the Conda and Jupyter Data Science Ecosystems},
    author={Tobias Fischer and Wolf Vollprecht and Silvio Traversaro and Sean Yen and Carlos Herrero and Michael Milford},
    journal={IEEE Robotics and Automation Magazine},
    year={2021},
    doi={10.1109/MRA.2021.3128367},
}
```

---

## For maintainers

### Layout

```
distros/<distro>/          only what is specific to one distribution
  distro.yaml              channel, upload target, conda-forge pinning version/migrations, own pins, rosdistro sync
  vinca.yaml               extends shared/vinca.yaml: ros_distro, build number, mutex version, its exceptions
  pkg_additional_info.yaml its own per-package settings (build numbers, version-specific cmake args)
  patch/                   its patches; patch/dependencies.yaml its own dependency fixes
  rosdistro_snapshot.yaml, rosdistro_additional_recipes.yaml
  ci.yaml                  temporary PR-build controls (full rebuild, cache evictions)
  conda_build_config.yaml  generated: `pixi run rs <distro> render-pinning`
  work/                    generated, git-ignored: where vinca and rattler-build run
shared/                    the same for every distribution
  vinca.yaml               settings and the package selection of every distribution
  pkg_additional_info.yaml per-package settings that are the same everywhere
  patch/dependencies.yaml  dependency fixes that are the same everywhere
  robostack.yaml           rosdep key -> conda package mapping (`robostack: []` drops a key)
  pinning/                 conda-forge pinning version, migrations and overrides
  tests/                   package tests (ros2-<pkg>.yaml; `if: ros_distro ...` for distro-specific bits)
tools/robostack.py         `pixi run rs ...`: every task, for one distribution or the repository
tools/maintenance.py       checks and robostack-bot commands
tools/import_distro.py     (re)import a distribution from its own repository
tools/*.py                 check_patches_clean_apply, check_dependency_compat, build_gap_report, ...
.scripts/                  staged build scripts (build_unix.sh, build_win.bat)
.github/workflows/         testpr.yaml, main.yaml (staged build branches), bot.yaml
.claude/skills/             procedures for agents (debug a build, patches, versions/rebuilds, package selection)
pixi.toml                  one environment for all distributions (one vinca, one rattler-build)
```

### How shared and distribution files combine

`distros/<distro>/vinca.yaml` starts with `extends: ../../shared/vinca.yaml`; vinca
combines the two, and the files next to each:

- **`vinca.yaml`:**
  - The package selection (`packages_select_by_deps`) lives in `shared/` for every distribution; vinca skips packages a distribution's rosdistro doesn't have.
  - `package_name_mode: new`: recipes and dependencies use `ros2-<pkg>` names only; no legacy alias packages are generated.
  - Exceptions, in `shared/` or a distribution: `packages_exclude` (not built, dropped from other packages' dependencies) and `packages_skip` (not built, dependents keep the dependency), each as plain names or under `- if: <condition>`. ROS packages mapped to conda-forge in `robostack.yaml` are skipped automatically.
  - `mutex_package`: name, `upper_bound` and the common `run_constraints` are shared. Each distribution sets its own `version`, and its `run_constraints` replace the shared constraint on the same package (e.g. jazzy's `libprotobuf 7.35.*`) or add new ones.
  - Other keys come from the distribution if it sets them, else from `shared/`. A distribution that differs from a shared setting just sets it.
  - Paths are relative to the file that sets them. `distros/<distro>/work/vinca.yaml` (generated) extends the distribution's file and adds `skip_existing` (its channel).
- **`pkg_additional_info.yaml` and `patch/dependencies.yaml`:** merged per package. The distribution's keys win, for example its own `build_number` on top of a shared `additional_cmake_args`.
- **`patch/*.patch` and `tests/`:** per package, the distribution's file replaces the shared one. In tests, `- if: ros_distro in ["humble", "jazzy"]` blocks select distribution-specific parts.
- **`vinca_pinning.yaml`:** the shared pinning, adjusted by `distro.yaml`:
  - `conda_forge_pinning_version` and `conda_forge_migrations` keep a distribution on its own conda-forge pinning, for example until its next full rebuild;
  - `pinning_overrides` replace shared pins by name, for example jazzy's Python 3.12.
  - `pixi run rs <distro> render-pinning` turns this into the committed `conda_build_config.yaml`.

A fix that applies to every distribution goes into `shared/`, a version-specific one into `distros/<distro>/`.

### Everyday tasks

```bash
pixi run rs jazzy generate-recipes            # vinca -m for jazzy
pixi run rs jazzy build                       # build everything missing on its channel
pixi run rs jazzy build-one ros2-ros-workspace
pixi run rs jazzy check-patches
pixi run rs jazzy check-deps                  # pin conflicts, before building anything
pixi run rs jazzy create-snapshot             # refresh rosdistro_snapshot.yaml
pixi run rs jazzy render-pinning              # after editing shared/pinning or distro.yaml
pixi run rs add-package foxglove_bridge humble jazzy
pixi run sort                                 # all YAML files
pixi run rs check                             # sanity checks (also run in CI)
```

**Rebuilding a package without bumping its build number:** add it to `evict_cache` in
`distros/<distro>/ci.yaml` for the PR, and reset the file after merging. A full rebuild
of a distribution uses `full_rebuild: true` there. A new build number goes into its
`pkg_additional_info.yaml` (one package) or `vinca.yaml` (everything).

### CI

- **Pull requests** (`testpr.yaml`):
  - A PR that only touches `distros/<distro>/` builds that distribution. Changes anywhere else (except documentation) build every distribution.
  - Each selected distribution is built on all five platforms, with its own build cache. The `check` job runs `pixi run rs check`.
- **After a merge** (`main.yaml`): for every changed distribution and platform, the job regenerates the recipes and the staged build workflow, and pushes them to `buildbranch_<distro>_<platform>`.
  - Each build branch carries its workflow as `.github/workflows/build.yaml`, which runs `.scripts/build_*.sh` in `distros/<distro>/work`.
  - Those workflows build the packages and upload them to the distribution's channel.
- **Repository variables:**
  - `ROBOSTACK_UPLOAD_CHANNEL`: upload to this prefix.dev channel instead (for example a test channel), or `none` to build without uploading. Use the canonical reference: `<namespace>/<channel>` for a channel that isn't the namespace's primary one (e.g. `tobias-fischer/robostack-distro-test`).
  - Upload credentials: prefix.dev channels use Repository Access (OIDC, no stored key). In the channel's *Settings > Repository Access*, authorize GitHub, this repository, workflow filename `build.yaml`, mode *Read/write*. The secret `PREFIX_API_KEY` is only needed for channels without Repository Access; while it is set, it is used instead. anaconda.org channels use `ANACONDA_API_TOKEN`.
  - `ROBOSTACK_BOT_APP_ID` with the secret `ROBOSTACK_BOT_PRIVATE_KEY`: the robostack-bot GitHub App. It pushes the build branches (which contain workflow files) and opens bot PRs so that CI runs on them. `GHA_PAT` works as a fallback.

  The app needs repository permissions Contents, Pull requests, Issues and Workflows (read and write), with its webhook inactive.

### robostack-bot

`.github/workflows/bot.yaml` runs `tools/maintenance.py`:

| command | who | what it does |
|---|---|---|
| `add-package <pkg>... [<distro>...]` | anyone | Adds ROS packages to the shared selection in `shared/vinca.yaml`, in sorted position; every distribution that has them builds them. Opens a PR listing, for the given (default: every) distribution, what will be built on linux-64 (and what is already published as a dependency). Valid ROS package names only, at most 10 per request. |
| `update-rosdistro-snapshot [<distro>... \| all]` | maintainers | Snapshots the distribution's latest rosdistro sync (the release team's tag, e.g. `jazzy/2026-10-05`, recorded as `rosdistro_sync` in `distro.yaml`; rosdistro master for `rosdistro_sync: manual`, like rolling), then sets `build_number` to the highest build of the distribution's released packages + 1, bumps the mutex minor version and removes all per-package build numbers: a full rebuild. The PR links the sync's announcement and lists the version bumps. Runs daily for distributions with a new sync. |
| `find-stale-packages [<distro>... \| all]` | maintainers | Published packages built against pins that no longer match, with the build-number snippet to rebuild them. Replies only. |
| `update-conda-forge-pinning [<distro>... \| all]` | maintainers | Moves the distribution's conda-forge pinning (`conda_forge_pinning_version` / `conda_forge_migrations` in `distro.yaml`, or `shared/pinning/conda_forge.yaml` for distributions that follow it) to the latest version, with the migrations that are done for the distribution's dependencies, and re-renders `conda_build_config.yaml`. Only packages that use a changed pin, and everything depending on them, are rebuilt (per-package build numbers, a new build of the mutex at the same version); more than half affected means a full rebuild. Mutex `run_constraints` of the form `<pkg> <version>.*` follow the new pins. The PR lists the rebuilt packages, the migrations used and those waiting for feedstocks, and the dependency check; it is labelled `dependency-conflict` when the pins can't be installed together. Runs daily, one PR per distribution. |

There are three ways to trigger a command:
- a *Package request* or *robostack-bot command* issue;
- a comment `@robostack-bot <command> ...` (no distribution or `all`: every distribution);
- *Actions › robostack-bot › Run workflow*.

"Maintainers" means the repository's owners, members and collaborators.

### New distribution

```bash
pixi run rs new-distro <name> --from rolling
```

This creates `distros/<name>/` with rolling's package selection and settings, but without its build numbers, patches or own pins. It also generates the snapshot and `conda_build_config.yaml`, and prints a checklist: mutex, channel, upload credentials, and patches to port.

### Moving a distribution here from its own repository

```bash
pixi run python tools/import_distro.py ../ros-<distro> --ref origin/main
pixi run rs <distro> render-pinning && pixi run sort && pixi run rs check
```

The import leaves out what `shared/` already provides (shared package selections and entries, derived keys) and keeps comments. It writes `distro.yaml` from the old repository's `pixi.toml` and `vinca_pinning.yaml`. Run it again to pick up changes made in the old repository until that is archived.
Imports validate and stage inputs before replacing the destination, and roll back
if replacement fails. The importer owns `vinca.yaml`, `distro.yaml`, `ci.yaml`,
`pkg_additional_info.yaml`, both rosdistro snapshot/additional-recipe files, and
the whole `patch/` directory. Optional files removed upstream are removed on
re-import. Unrelated local entries, including `work/`, `tests/`, and rendered pins,
are preserved. `--distro` changes the destination identity; legacy package names
are still converted using the source distribution's name.
Modern package lists retain scalar and nested `if`/`then`/`else` branches, including
empty branches; importing does not resolve away platform-specific selections.


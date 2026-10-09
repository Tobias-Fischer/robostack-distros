#!/usr/bin/env python3
"""
check_orphaned_platform_patches.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Detect effective patch files that Vinca will silently never wire into a
recipe because the same package uses conflicting filename prefixes.

Background
----------
Vinca (``vinca/configuration.py::_discover_patches`` and
``vinca/utils.py::add_package_name_variants``) builds a dict keyed by
the patch filename's prefix (everything before an optional
``.osx``/``.win``/``.linux``/``.unix``/``.emscripten`` suffix), then
cross-links name-prefix variants of the *same* logical package
(``X`` <-> ``ros-X`` <-> ``ros2-X`` <-> ``ros-<distro>-X``) via
``dict.setdefault()``.

``setdefault`` only fills in a key that is still *absent*. If a
package has a plain patch under one prefix (say ``ros2-foo.patch``)
and a platform-specific patch under a *different* prefix (say
``ros-jazzy-foo.osx.patch``), both prefixes already exist as their own
dict entries by the time the cross-link step runs, so the two never
merge. Whichever prefix vinca does *not* resolve as the package's
final conda name for a given recipe simply never appears in that
recipe's ``patches:`` list -- with no error and no warning. This
exact bug orphaned ``ros-jazzy-sick-scan-xd.osx.patch`` for months
before it was renamed to ``ros2-sick-scan-xd.osx.patch`` (matching the
prefix jazzy actually resolves sick_scan_xd's own patch under).

``check_patches_clean_apply.py`` checks effective patch sets for generated
recipes, but cannot detect patches hidden behind a different name prefix.

What this script does
----------------------
Resolve the source configuration with Vinca for every supported target,
including inherited directories and distro overrides. Inspect the literal
filename prefixes of effective patches, not Vinca's synthetic alias keys.
Flag packages with multiple prefixes on the same target; shadowed shared
patches and different target-specific configurations are not collisions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from patches import resolved_patch_configs
from vinca.utils import add_package_name_variants


def shortname_of(name: str, ros_distro: str) -> str:
    """Use Vinca's own alias expansion to normalize a literal filename prefix."""
    aliases = {name: None}
    add_package_name_variants(aliases, ros_distro)
    legacy_prefix = f"ros-{ros_distro}-"
    return next(alias[len(legacy_prefix):] for alias in aliases if alias.startswith(legacy_prefix))


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect orphaned platform patches.")
    parser.add_argument(
        "--vinca", type=Path, default=Path("vinca.yaml"),
        help="Vinca configuration including inherited patches (default: ./vinca.yaml)",
    )
    args = parser.parse_args()
    scanned: set[Path] = set()
    collisions: dict[tuple[str, str], dict[str, set[Path]]] = {}
    for platform, config in resolved_patch_configs(args.vinca):
        groups: dict[str, dict[str, set[Path]]] = {}
        # Alias collisions happen before Vinca selects a platform bucket:
        # ros2-demo.linux.patch can hide ros-jazzy-demo.osx.patch on macOS.
        files = {
            Path(path)
            for buckets in config["_patches"].values()
            for paths in buckets.values()
            for path in paths
        }
        for path in files:
            scanned.add(path)
            prefix = path.name.split(".")[0]
            shortname = shortname_of(prefix, config["ros_distro"])
            groups.setdefault(shortname, {}).setdefault(prefix, set()).add(path)
        for shortname, prefixes in groups.items():
            if len(prefixes) > 1:
                collisions[platform, shortname] = prefixes

    if not collisions:
        print(f"OK: no orphaned platform-specific patches ({len(scanned)} effective patch files scanned).")
        return 0

    print(
        "ORPHANED PLATFORM PATCH RISK: the following packages have patch files "
        "spread across more than one name-prefix variant. vinca's "
        "add_package_name_variants() cross-links prefix variants via "
        "dict.setdefault(), which is a no-op once a variant already exists as its "
        "own entry -- so only ONE of the prefixes below will end up attached to "
        "the package's real generated recipe; any platform-specific patch under "
        "the others is silently never applied.\n",
        file=sys.stderr,
    )
    for (platform, shortname), prefixes in sorted(collisions.items()):
        print(f"  {shortname} ({platform}):", file=sys.stderr)
        for prefix, files in sorted(prefixes.items()):
            print(f"    {prefix}: {', '.join(str(path) for path in sorted(files))}", file=sys.stderr)
    print(
        "\nFix: rename the patch file(s) so every file for a given package shares "
        "the SAME name prefix (matching whichever prefix that package's own "
        "recipe.yaml actually resolves to -- check recipes/<pkg>/*/recipe.yaml's "
        "source.patches entries, or regenerate recipes locally and inspect).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

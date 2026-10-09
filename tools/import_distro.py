"""(Re)import a distribution from its own repository (RoboStack/ros-<distro>).

    pixi run python tools/import_distro.py ../ros-jazzy [--ref origin/main] [--distro jazzy]

Used to move a distribution into this repository, and to pick up changes made in
its old repository until it is archived. It copies the distribution's data into
distros/<distro>/ and leaves out what shared/ already provides:

- vinca.yaml               extending shared/vinca.yaml, without its package selections,
                           with this repository's paths (patch_dir, snapshot paths;
                           skip_existing is derived); comments are kept
- pkg_additional_info.yaml, patch/dependencies.yaml
                           without entries identical to shared/ (build numbers stay);
                           ros-<distro>-<pkg> dependency names become ros2-<pkg>
- patch/                   patches renamed to ros2-<pkg>[.<platform>].patch
- rosdistro_snapshot.yaml, rosdistro_additional_recipes.yaml   as they are
- distro.yaml              channel and upload target (from its pixi.toml), its
                           conda-forge pinning version, migrations and the pins that
                           differ from shared/pinning/overrides.yaml (vinca_pinning.yaml)
- ci.yaml                  reset to the defaults

Reimports replace these importer-owned files and the entire patch/ tree, including
local edits; absent optional files are removed. Other distro-local entries (such
as work/, tests/, and rendered pinning) are retained without copying them. Inputs
are staged and validated first; replacement uses same-filesystem renames with
rollback on failure, rather than replacing a nonempty directory on Windows.

Afterwards: `pixi run rs <distro> render-pinning`, `pixi run sort`, `pixi run rs check`.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

import yaml
from ruamel.yaml import YAML

import robostack as rs

DERIVED_KEYS = ("conda_index", "patch_dir", "rosdistro_snapshot", "rosdistro_additional_recipes", "skip_existing")
OWNED_FILES = {
    "vinca.yaml", "pkg_additional_info.yaml", "patch",
    "rosdistro_snapshot.yaml", "rosdistro_additional_recipes.yaml",
    "distro.yaml", "ci.yaml",
}


def _ry() -> YAML:
    ry = YAML()
    ry.preserve_quotes = True
    ry.width = 4096
    ry.indent(mapping=2, sequence=4, offset=2)
    return ry


def _norm(x):
    if isinstance(x, str):
        return x.replace("-", "_")
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_norm(v) for v in x]
    return x


def _key(x) -> str:
    return json.dumps(_norm(json.loads(json.dumps(x))), sort_keys=True)


def _entries(items) -> set[tuple[str, str | None]]:
    out = set()
    for item in items or []:
        if isinstance(item, str):
            out.add((item.replace("-", "_"), None))
        else:
            out |= {(n.replace("-", "_"), str(item["if"])) for n in item.get("then") or []}
    return out


def _keep(items, wanted: set) -> list:
    """The entries of a package list (plain names / if-blocks) that are in wanted."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            if (item.replace("-", "_"), None) in wanted:
                out.append(item)
        else:
            then = [n for n in item.get("then") or [] if (n.replace("-", "_"), str(item["if"])) in wanted]
            if then:
                item["then"] = then
                for extra in [k for k in item if k not in ("if", "then")]:
                    del item[extra]
                out.append(item)
    return out


def _convert_exclusions(vinca) -> None:
    """vinca's packages_skip_by_deps / packages_remove_from_deps -> packages_exclude
    (in the remove list) and packages_skip (skip only)."""
    skip = vinca.pop("packages_skip_by_deps", None) or []
    remove = vinca.pop("packages_remove_from_deps", None) or []
    skip_only = _entries(skip) - _entries(remove)
    if remove:
        vinca["packages_exclude"] = remove
    if skip_only:
        vinca["packages_skip"] = _keep(skip, skip_only)


class Source:
    """Files of the old repository, from a working tree or a git ref."""

    def __init__(self, repo: Path, ref: str | None):
        self.repo, self.ref = repo, ref

    def text(self, path: str) -> str | None:
        if self.ref is None:
            p = self.repo / path
            return p.read_text() if p.exists() else None
        # A missing optional file is different from a failed Git operation.
        listing = subprocess.run(
            ["git", "-C", str(self.repo), "ls-tree", self.ref, "--", path],
            capture_output=True, text=True, check=True,
        )
        if not listing.stdout:
            return None
        return subprocess.run(
            ["git", "-C", str(self.repo), "show", f"{self.ref}:{path}"],
            capture_output=True, text=True, check=True,
        ).stdout

    def copy_dir(self, path: str, dest: Path) -> None:
        if self.ref is None:
            shutil.copytree(self.repo / path, dest / path, dirs_exist_ok=True)
            return
        data = subprocess.run(["git", "-C", str(self.repo), "archive", self.ref, path], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(dest, filter="data")


def _mapping(text: str | None, name: str, *, required: bool = False):
    if text is None:
        if required:
            raise ValueError(f"Missing required input: {name}")
        return {}
    data = _ry().load(text)
    if data is None and not required:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{name} must contain a YAML mapping")
    return data


def _distro_name(value: str | None) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
        raise ValueError(f"Invalid ros_distro name: {value!r}")
    return value


def _validate_package_entries(value, key: str) -> None:
    """Validate modern package lists without evaluating away selector branches."""
    if value is None or isinstance(value, str):
        return
    if isinstance(value, list):
        for item in value:
            _validate_package_entries(item, key)
        return
    if isinstance(value, dict) and "if" in value and "then" in value:
        _validate_package_entries(value["then"], key)
        _validate_package_entries(value.get("else"), key)
        return
    raise ValueError(f"vinca.yaml {key} must contain package names or if/then selectors")


def _build_import(src: Source, distro: str, source_distro: str, vinca, dest: Path) -> None:
    ry = _ry()
    for key in (*rs.LIST_KEYS, "packages_skip_by_deps", "packages_remove_from_deps"):
        items = vinca.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            raise ValueError(f"vinca.yaml {key} must be a list")
        if key in rs.LIST_KEYS:
            _validate_package_entries(items, key)
            continue
        # Legacy exclusion conversion below expects flat if/then lists.
        for item in items:
            if isinstance(item, str):
                continue
            if (not isinstance(item, dict) or "if" not in item
                    or not isinstance(item.get("then"), list)
                    or any(not isinstance(name, str) for name in item["then"])):
                raise ValueError(f"vinca.yaml {key} must contain package names or if/then lists")
    snapshots = {
        name: src.text(name)
        for name in ("rosdistro_snapshot.yaml", "rosdistro_additional_recipes.yaml")
    }
    for name, text in snapshots.items():
        data = _mapping(text, name, required=name == "rosdistro_snapshot.yaml")
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in data.items()):
            raise ValueError(f"{name} must map package names to metadata mappings")
    # vinca.yaml: drop what shared/ and the tooling provide
    shared = _mapping((rs.SHARED / "vinca.yaml").read_text(), "shared/vinca.yaml", required=True)
    for key in DERIVED_KEYS:
        vinca.pop(key, None)
    # on top of shared/vinca.yaml, with its own patches and snapshot
    for i, (key, value) in enumerate([
        ("extends", "../../shared/vinca.yaml"),
        ("ros_distro", distro),
        ("patch_dir", "patch"),
        ("rosdistro_snapshot", "rosdistro_snapshot.yaml"),
        ("rosdistro_additional_recipes",
         "rosdistro_additional_recipes.yaml" if snapshots["rosdistro_additional_recipes.yaml"] is not None else None),
    ]):
        vinca.pop(key, None)
        vinca.insert(i, key, value)
    # This repository has cut over to ros2-* names; never import legacy aliases.
    vinca.pop("package_name_mode", None)
    if shared.get("package_name_mode") != "new":
        vinca.insert(5, "package_name_mode", "new")
    _convert_exclusions(vinca)
    for key in rs.LIST_KEYS:
        common = {_key(i) for i in shared.get(key) or []}
        items = vinca.get(key)
        if items is None:
            continue
        for i in reversed(range(len(items))):
            if _key(items[i]) in common:
                del items[i]
    with (dest / "vinca.yaml").open("w") as fh:
        ry.dump(vinca, fh)

    # as they are (patch/dependencies.yaml is filtered below)
    src.copy_dir("patch", dest)
    if not (dest / "patch").is_dir():
        raise ValueError("Missing required input: patch/")
    for patch in (dest / "patch").glob(f"ros-{source_distro}-*.patch"):
        renamed = patch.with_name("ros2-" + patch.name.removeprefix(f"ros-{source_distro}-"))
        if renamed.exists():
            raise ValueError(f"Patch rename would overwrite {renamed.name}")
        patch.rename(renamed)
    for name, text in snapshots.items():
        if text is not None:
            (dest / name).write_text(text)

    # per-package files: drop entries identical to shared/ (keeping build numbers)
    for name, shared_file, target in (
        ("pkg_additional_info.yaml", "pkg_additional_info.yaml", dest / "pkg_additional_info.yaml"),
        ("patch/dependencies.yaml", "patch/dependencies.yaml", dest / "patch" / "dependencies.yaml"),
    ):
        text = src.text(name)
        if text is None:
            continue
        text = re.sub(rf"\bros-{re.escape(source_distro)}-(?=[a-z0-9])", "ros2-", text)
        data = _mapping(text, name)
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in data.items()):
            raise ValueError(f"{name} must map package names to settings mappings")
        common = _mapping((rs.SHARED / shared_file).read_text(), f"shared/{shared_file}")
        for key in list(data):
            if key not in common:
                continue
            entry = json.loads(json.dumps(data[key]))
            rest = {k: v for k, v in entry.items() if k != "build_number"} if isinstance(entry, dict) else entry
            if rest == common[key]:
                if isinstance(entry, dict) and "build_number" in entry:
                    for k in [k for k in data[key] if k != "build_number"]:
                        del data[key][k]
                else:
                    del data[key]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w") as fh:
            ry.dump(data, fh)

    # distro.yaml
    pixi = src.text("pixi.toml") or ""
    tomllib.loads(pixi)
    settings: dict = {}
    if m := re.search(r'upload\s*=\s*"rattler-build upload prefix -c ([^\s"]+)', pixi):
        settings.update(channel_name=m.group(1), upload_target="prefix")
    elif m := re.search(r'upload\s*=\s*"rattler-build upload anaconda -o ([^\s"]+)', pixi):
        settings.update(channel_name=m.group(1), upload_target="anaconda")
    pinning = _mapping(src.text("vinca_pinning.yaml"), "vinca_pinning.yaml")
    if pinning.get("migrations") is not None and not isinstance(pinning["migrations"], list):
        raise ValueError("vinca_pinning.yaml migrations must be a list")
    if pinning.get("pinning_overrides") is not None and not isinstance(pinning["pinning_overrides"], dict):
        raise ValueError("vinca_pinning.yaml pinning_overrides must be a mapping")
    if pinning.get("conda_forge_pinning_version"):
        settings["conda_forge_pinning_version"] = str(pinning["conda_forge_pinning_version"])
        settings["conda_forge_migrations"] = list(pinning.get("migrations") or [])
        shared_overrides = yaml.safe_load((rs.SHARED / "pinning" / "overrides.yaml").read_text())["pinning_overrides"]
        own = {k: v for k, v in (pinning.get("pinning_overrides") or {}).items()
               if k not in shared_overrides or shared_overrides[k] != v}
        if own:
            settings["pinning_overrides"] = own
    (dest / "distro.yaml").write_text(
        f"# Settings of ros-{distro} that differ from the other distributions\n"
        "# (everything else is shared, see the README).\n"
        + yaml.safe_dump(json.loads(json.dumps(settings)), sort_keys=False, default_flow_style=None, width=100)
    )
    ci = (rs.ROOT / "tools" / "ci_default.yaml").read_text()
    _mapping(ci, "tools/ci_default.yaml", required=True)
    (dest / "ci.yaml").write_text(ci)


def _publish(staged: Path, dest: Path, backup: Path) -> None:
    if not dest.exists():
        staged.rename(dest)
        return
    preserved = []
    dest.rename(backup)
    try:
        # Moving these entries preserves large work trees, symlinks and metadata.
        for entry in backup.iterdir():
            if entry.name not in OWNED_FILES:
                entry.rename(staged / entry.name)
                preserved.append(entry.name)
        staged.rename(dest)
    except BaseException:
        try:
            for name in reversed(preserved):
                (staged / name).rename(backup / name)
            backup.rename(dest)
        except BaseException as rollback_error:
            raise RuntimeError(
                f"Import rollback failed; retained all recovery data in {staged.parent}"
            ) from rollback_error
        raise


def import_distro(src: Source, distro: str | None) -> str:
    vinca = _mapping(src.text("vinca.yaml"), "vinca.yaml", required=True)
    source_distro = _distro_name(vinca.get("ros_distro"))
    distro = _distro_name(distro if distro is not None else source_distro)
    dest = rs.DISTROS / distro
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        raise ValueError(f"Import destination must be a real directory: {dest}")
    for name in OWNED_FILES - {"patch"}:
        if (dest / name).is_dir():
            raise ValueError(f"Refusing to replace a directory with an imported file: {dest / name}")

    rs.DISTROS.mkdir(parents=True, exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix=f".import-{distro}-", dir=rs.DISTROS))
    staged, backup = transaction / "new", transaction / "previous"
    staged.mkdir()
    published = False
    try:
        _build_import(src, distro, source_distro, vinca, staged)
        _publish(staged, dest, backup)
        published = True
    finally:
        # Failed rollback must never cause cleanup to delete preserved user data.
        if published or not backup.exists():
            try:
                shutil.rmtree(transaction)
            except OSError as error:
                print(f"Warning: could not remove import staging directory {transaction}: {error}", file=sys.stderr)
    return distro


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "Reimports overwrite local edits to vinca.yaml, distro.yaml, ci.yaml, "
            "snapshot/additional-recipes YAML, pkg_additional_info.yaml and all of patch/. "
            "Missing optional inputs remove their old copies. Other distro-local files, "
            "including work/, tests/ and rendered pinning, are preserved without copying. "
            "Inputs are validated before replacement; failed replacement is rolled back."
        ),
    )
    parser.add_argument("repo", type=Path, help="checkout of RoboStack/ros-<distro>")
    parser.add_argument("--ref", help="git ref to import (default: the working tree)")
    parser.add_argument("--distro", help="default: ros_distro of its vinca.yaml")
    args = parser.parse_args()
    distro = import_distro(Source(args.repo.resolve(), args.ref), args.distro)
    print(f"imported distros/{distro}; now run: pixi run rs {distro} render-pinning && pixi run sort && pixi run rs check")
    return 0


if __name__ == "__main__":
    sys.exit(main())

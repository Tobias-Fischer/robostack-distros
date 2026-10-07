"""Maintenance commands (used by robostack-bot, .github/workflows/bot.yml).

    pixi run rs check                                 # sanity checks of the whole repository
    pixi run rs <distro> update-snapshot              # refresh rosdistro_snapshot.yaml, summarise bumps
    pixi run rs <distro> find-stale                   # published packages built against outdated pins
    pixi run rs update-pinning                        # move shared/pinning/conda_forge.yaml forward
    pixi run rs new-distro NAME --from DISTRO         # add distros/NAME, seeded from DISTRO
    pixi run rs add-package PKG... [DISTRO...] [--preview]   # select packages for building
    pixi run rs parse-command --body TEXT --association ROLE   # for @robostack-bot comments

Each command prints a markdown summary; `--summary FILE` also writes it to FILE and,
in GitHub Actions, sets the outputs title / changed / ok.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

import robostack as rs

BOT_COMMANDS = {
    "add-package": "add ROS packages to the package selection (opens a PR; anyone can ask)",
    "update-rosdistro-snapshot": "refresh the distribution's rosdistro_snapshot.yaml (opens a PR)",
    "find-stale-packages": "list its published packages built against outdated pins",
    "update-conda-forge-pinning": "move the shared conda-forge pinning to the latest version (opens a PR)",
}
PER_DISTRO_COMMANDS = ("update-rosdistro-snapshot", "find-stale-packages")
ALLOWED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass
class Result:
    title: str
    summary: str
    changed: bool = False
    ok: bool = True


def tail(text: str, lines: int = 80) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# --------------------------------------------------------------------------- #
# per distribution
# --------------------------------------------------------------------------- #
def _versions(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text()) if path.is_file() else {}
    return {k: str(v.get("version")) for k, v in (data or {}).items() if isinstance(v, dict)}


def snapshot_changes(old: dict[str, str], new: dict[str, str]) -> str:
    """Counts up front, the lists folded away (they run to hundreds of lines)."""
    added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
    bumped = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    lines = [f"{len(bumped)} updated, {len(added)} added, {len(removed)} removed packages."]

    def fold(title: str, body: list[str]) -> None:
        lines.extend(["", f"<details><summary>{title}</summary>", "", *body, "", "</details>"])

    if bumped:
        fold(f"Updated ({len(bumped)})", ["| package | old | new |", "|---|---|---|"]
             + [f"| {k} | {old[k]} | {new[k]} |" for k in bumped])
    if added:
        fold(f"Added ({len(added)})", [", ".join(f"`{k}`" for k in added)])
    if removed:
        fold(f"Removed ({len(removed)})", [", ".join(f"`{k}`" for k in removed)])
    return "\n".join(lines)


def _ruamel():
    from ruamel.yaml import YAML

    ry = YAML()
    ry.preserve_quotes = True
    ry.width = 4096
    ry.indent(mapping=2, sequence=4, offset=2)
    return ry


def sync_mutex_constraints(distro: str) -> list[str]:
    """Move `<pkg> <version>.*` run_constraints of the mutex to the rendered pin of
    the same package in conda_build_config.yaml. Other constraints stay as they are."""
    path = rs.DISTROS / distro / "vinca.yaml"
    pins = yaml.safe_load((rs.DISTROS / distro / "conda_build_config.yaml").read_text()) or {}
    changes: list[str] = []

    def repl(m: re.Match) -> str:
        name, old = m.group(2), m.group(3)
        pin = pins.get(name.replace("-", "_"))
        if not (isinstance(pin, list) and pin):
            return m.group(0)
        # same precision as before (`libprotobuf 7.35.*` stays at major.minor)
        new = ".".join(str(pin[0]).split(".")[: len(old.split("."))])
        if new == old:
            return m.group(0)
        changes.append(f"`{name}` {old}.* → {new}.*")
        return f"{m.group(1)}{name} {new}.*"

    text = path.read_text()
    head, sep, rest = text.partition("run_constraints:")
    if sep:
        block = re.match(r"(?:[ \t]*(?:-.*|#.*)?\n)*", rest).group(0)
        block_new = re.sub(r"(?m)^([ \t]*-[ \t]+)([A-Za-z0-9_.-]+) ([0-9][0-9.]*)\.\*[ \t]*$", repl, block)
        path.write_text(head + sep + block_new + rest[len(block):])
    return changes


REPODATA_SUBDIRS = ("noarch", "linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64")


def released_build_number(distro: str) -> int | None:
    """Highest build number of the distribution's packages on its channel (the mutex
    aside), or None when the channel can't be read."""
    import urllib.error
    import urllib.request

    s = rs.settings(distro)
    name = s.get("channel_name", f"robostack-{distro}")
    base = f"https://repo.prefix.dev/{name}" if s.get("upload_target", "prefix") == "prefix" else rs.channel_url(distro)
    mutex = (yaml.safe_load((rs.DISTROS / distro / "vinca.yaml").read_text()).get("mutex_package") or {}).get("name")
    prefixes = (f"ros-{distro}-", "ros2-")
    highest, read = None, False
    for subdir in REPODATA_SUBDIRS:
        try:
            request = urllib.request.Request(f"{base}/{subdir}/repodata.json", headers={"User-Agent": "robostack-bot"})
            with urllib.request.urlopen(request, timeout=300) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        read = True
        for record in [*data.get("packages", {}).values(), *data.get("packages.conda", {}).values()]:
            if record["name"].startswith(prefixes) and record["name"] != mutex:
                highest = max(highest or 0, int(record.get("build_number", 0)))
    return highest if read else None


def bump_rebuild(distro: str) -> list[str]:
    """Rebuild everything: build_number = highest released build number + 1 (so every
    package gets a new build, whatever the current value), mutex minor + 1, and no
    per-package build numbers."""
    path = rs.DISTROS / distro / "vinca.yaml"
    text = path.read_text()
    current = int(re.search(r"(?m)^build_number:\s*(\d+)", text).group(1))
    released = released_build_number(distro)
    number = current + 1 if released is None else released + 1
    text = re.sub(r"(?m)^(build_number:\s*)\d+", rf"\g<1>{number}", text, count=1)
    m = re.search(r"(?m)^mutex_package:\n(?:[ \t]+.*\n|\n)*?[ \t]+version:[ \t]*[\"']?(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        raise SystemExit(f"{path}: no mutex_package version")
    old_mutex = ".".join(m.group(1, 2, 3))
    new_mutex = f"{m.group(1)}.{int(m.group(2)) + 1}.0"
    text = text[:m.start(1)] + new_mutex + text[m.end(3):]
    path.write_text(text)
    source = "channel unreadable, current + 1" if released is None else f"highest released build {released} + 1"
    lines = [f"`build_number` {current} → {number} ({source}), mutex {old_mutex} → {new_mutex} (rebuilds every package)"]

    info = rs.DISTROS / distro / "pkg_additional_info.yaml"
    if info.is_file():
        ry = _ruamel()
        data = ry.load(info.read_text()) or {}
        dropped = []
        for pkg in list(data):
            entry = data[pkg]
            if isinstance(entry, dict) and "build_number" in entry:
                dropped.append(f"`{pkg}` ({entry['build_number']})")
                del entry["build_number"]
                if not entry:
                    del data[pkg]
        if dropped:
            with info.open("w") as fh:
                ry.dump(data, fh)
            lines.append(f"removed {len(dropped)} per-package build numbers: " + ", ".join(dropped))
    return lines


def update_snapshot(distro: str) -> Result:
    snapshot = rs.DISTROS / distro / "rosdistro_snapshot.yaml"
    old = _versions(snapshot)
    if rs.task(distro, "create-snapshot", []):
        return Result(f"{distro}: snapshot update failed", "`vinca-snapshot` failed, see the log.", ok=False)
    new = _versions(snapshot)
    if old == new:
        return Result(f"{distro}: snapshot is up to date", "No package versions changed.")
    bump = bump_rebuild(distro)
    return Result(f"{distro}: update rosdistro snapshot",
                  snapshot_changes(old, new) + "\n\n" + "\n".join(f"- {l}" for l in bump), changed=True)


def find_stale(distro: str) -> Result:
    s = rs.settings(distro)
    name = s.get("channel_name", f"robostack-{distro}")
    # repo.prefix.dev serves repodata directly (prefix.dev redirects)
    url = f"https://repo.prefix.dev/{name}" if s.get("upload_target", "prefix") == "prefix" else rs.channel_url(distro)
    rs.prepare(distro)
    proc = subprocess.run(
        [sys.executable, str(rs.TOOLS / "check_dependency_compat.py"), "--stale", "--repodata", url],
        cwd=rs.work_dir(distro), capture_output=True, text=True,
    )
    output = tail(proc.stdout + proc.stderr, 200)
    if "Traceback (most recent call last)" in output:
        return Result(f"{distro}: find-stale-packages failed", f"```\n{output}\n```", ok=False)
    stale = proc.returncode != 0
    return Result(
        f"{distro}: {'stale packages' if stale else 'no stale packages'}",
        f"`check_dependency_compat.py --stale --repodata {url}`\n\n"
        f"<details><summary>Report</summary>\n\n```\n{output}\n```\n\n</details>",
        ok=not stale,
    )


# --------------------------------------------------------------------------- #
# whole repository
# --------------------------------------------------------------------------- #
def update_pinning() -> Result:
    """Latest conda-forge pinning for everyone that follows the shared one; migrations
    are selected for the dependencies of all distributions."""
    from vinca import pinning

    shared = rs.SHARED / "pinning" / "conda_forge.yaml"
    before = yaml.safe_load(shared.read_text()) or {}
    dependencies: set[str] = set()
    for distro in rs.distros():
        print(f"Collecting dependencies of {distro}", flush=True)
        dependencies |= pinning.dependencies_from_vinca(rs.prepare(distro), pinning.DEFAULT_PLATFORMS)
    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "vinca_pinning.yaml"
        spec.write_text(shared.read_text() + "\n" + (rs.SHARED / "pinning" / "overrides.yaml").read_text())
        version, migrations, reports = pinning.update_pinning(spec, dependencies=dependencies)
    if str(before.get("conda_forge_pinning_version")) == str(version) and list(before.get("migrations") or []) == list(migrations):
        return Result("Pinning is up to date", f"Already on conda-forge-pinning {version}.")
    header = [l for l in shared.read_text().splitlines() if l.startswith("#")]
    body = [f"conda_forge_pinning_version: {version}", "migrations:"] + [f"  - {m}" for m in migrations]
    shared.write_text("\n".join(header + body) + "\n")
    followers = [d for d in rs.distros() if not rs.settings(d).get("conda_forge_pinning_version")]
    rebuilds: list[str] = []
    for distro in followers:
        cbc = rs.DISTROS / distro / "conda_build_config.yaml"
        before_cbc = cbc.read_text() if cbc.is_file() else ""
        rs.task(distro, "render-pinning", [])
        if cbc.read_text() == before_cbc:
            rebuilds.append(f"- **{distro}**: pins unchanged, no rebuild")
            continue
        constraints = sync_mutex_constraints(distro)
        rebuilds.append(f"- **{distro}**: " + "; ".join(bump_rebuild(distro)))
        if constraints:
            rebuilds.append("  - mutex run_constraints: " + ", ".join(constraints))
    lines = [
        f"Moved `shared/pinning/conda_forge.yaml` to conda-forge-pinning `{version}`, with migrations "
        "selected for the dependencies of every distribution.",
        "",
        "Applied migrations: " + (", ".join(f"`{m}`" for m in migrations) or "none"),
        "",
        *[f"- `{name}`: {report}" for name, report in reports],
        "",
        "Re-rendered `conda_build_config.yaml` of: " + (", ".join(followers) or "none")
        + " (the others pin their own version in `distro.yaml`).",
        "",
        *rebuilds,
        "",
        "Check the mutex `run_constraints` that aren't plain pins with `pixi run rs <distro> check-deps`.",
    ]
    return Result("Update conda-forge pinning", "\n".join(lines), changed=True)


def new_distro(name: str, source: str) -> Result:
    """distros/NAME from the template of an existing distribution: its package
    selection and settings, but no build numbers, patches or own pins."""
    src, dest = rs.DISTROS / source, rs.DISTROS / name
    if dest.exists():
        raise SystemExit(f"{dest} exists")
    dest.mkdir(parents=True)
    settings = rs.settings(source)
    keep = {k: settings[k] for k in ("upload_target",) if k in settings}
    (dest / "distro.yaml").write_text(
        f"# Settings of ros-{name} that differ from the other distributions\n"
        "# (everything else is shared, see the README).\n"
        + yaml.safe_dump({"channel_name": f"robostack-{name}", **keep}, sort_keys=False)
    )
    vinca = (src / "vinca.yaml").read_text()
    vinca = re.sub(r"(?m)^ros_distro:.*$", f"ros_distro: {name}", vinca)
    vinca = re.sub(r"(?m)^build_number:.*$", "build_number: 0", vinca)
    vinca = re.sub(r"(?m)^package_name_mode:.*\n", "", vinca)  # shared default: both
    (dest / "vinca.yaml").write_text(vinca)
    info = yaml.safe_load((src / "pkg_additional_info.yaml").read_text()) or {}
    info = {k: {a: b for a, b in v.items() if a != "build_number"} for k, v in info.items() if isinstance(v, dict)}
    (dest / "pkg_additional_info.yaml").write_text(yaml.safe_dump({k: v for k, v in info.items() if v}, sort_keys=True))
    (dest / "rosdistro_additional_recipes.yaml").write_text("{}\n")
    (dest / "patch").mkdir()
    (dest / "ci.yaml").write_text(_default_ci_yaml())
    steps = [rs.task(name, "create-snapshot", []), rs.task(name, "render-pinning", [])]
    patches = sorted(p.name for p in (src / "patch").glob("*.patch"))
    lines = [
        f"Created `distros/{name}/` from `distros/{source}/` "
        f"(snapshot: {'ok' if not steps[0] else 'FAILED'}, pinning: {'ok' if not steps[1] else 'FAILED'}).",
        "",
        "Next steps:",
        f"- [ ] review `distros/{name}/vinca.yaml` (mutex name/version, package selection)",
        f"- [ ] create the `robostack-{name}` channel and its trusted publisher / upload token",
        f"- [ ] port patches that still apply ({len(patches)} in `distros/{source}/patch/`), "
        f"check with `pixi run rs {name} check-patches`",
    ]
    return Result(f"New distribution {name}", "\n".join(lines), changed=True, ok=not any(steps))


PACKAGE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")  # valid ROS package names only
MAX_PACKAGES = 10  # per request


def _package_name(name: str) -> str:
    """ROS package name from a request: `ros-humble-foo-bar`, `ros2-foo-bar`, `foo-bar` -> foo_bar."""
    name = name.strip().strip("`'\",.").lower()
    name = re.sub(r"^(ros2-|ros-[a-z]+-)", "", name)
    return name.replace("-", "_")


def _selected(distro: str) -> set[str]:
    """Packages the distribution selects (unconditionally or on some platforms)."""
    vinca = yaml.safe_load((rs.prepare(distro) / "vinca.yaml").read_text())
    names: set[str] = set()

    def walk(items):
        for item in items or []:
            if isinstance(item, str):
                names.add(item.replace("-", "_"))
            elif isinstance(item, dict):
                walk(item.get("then"))
                walk(item.get("else"))

    walk(vinca.get("packages_select_by_deps"))
    return names


def _available(distro: str) -> set[str]:
    d = rs.DISTROS / distro
    names = set(yaml.safe_load((d / "rosdistro_snapshot.yaml").read_text()) or {})
    extra = d / "rosdistro_additional_recipes.yaml"
    if extra.is_file():
        names |= set(yaml.safe_load(extra.read_text()) or {})
    return names


def _append_selection(path: Path, packages: list[str]) -> None:
    """Append to packages_select_by_deps keeping the file's comments (round trip)."""
    ry = _ruamel()
    data = ry.load(path.read_text()) if path.is_file() else {}
    data.setdefault("packages_select_by_deps", [])
    items = data["packages_select_by_deps"]
    for pkg in packages:
        # into the run of plain (unconditional) names, in sorted position
        index = 0
        while (index < len(items) and isinstance(items[index], str)
               and items[index].replace("-", "_").lower() < pkg.lower()):
            index += 1
        items.insert(index, pkg)
    with path.open("w") as fh:
        ry.dump(data, fh)


def add_package(packages: list[str], distros: list[str], preview: bool = False) -> Result:
    """Select ROS packages for building, in the given (default: all) distributions
    that have them, and list the recipes this adds on linux-64."""
    wanted = list(dict.fromkeys(n for n in (_package_name(p) for p in packages if p.strip()) if PACKAGE_NAME.match(n)))
    wanted = wanted[:MAX_PACKAGES]
    if not wanted:
        return Result("add-package: no package given", "Name at least one ROS package.", ok=False)
    targets = distros or rs.distros()
    plan: dict[str, list[str]] = {}
    lines: list[str] = []
    for distro in targets:
        available, selected = _available(distro), _selected(distro)
        add = []
        for pkg in wanted:
            if pkg not in available:
                lines.append(f"- **{distro}**: `{pkg}` is not in its rosdistro")
            elif pkg in selected:
                lines.append(f"- **{distro}**: `{pkg}` is already selected")
            else:
                add.append(pkg)
        if add:
            plan[distro] = add
    if not plan:
        return Result(f"add-package {' '.join(wanted)}: nothing to add", "\n".join(lines))

    # every distribution gets the same packages -> shared/vinca.yaml, else per distribution
    everywhere = set(plan) == set(rs.distros()) and len({tuple(v) for v in plan.values()}) == 1
    backup = {p: p.read_bytes() for p in [rs.SHARED / "vinca.yaml"] + [rs.DISTROS / d / "vinca.yaml" for d in plan]}
    if everywhere:
        _append_selection(rs.SHARED / "vinca.yaml", next(iter(plan.values())))
        subprocess.run(["vinca-sort-vinca-lists", "shared/vinca.yaml"], cwd=rs.ROOT, check=True)
        where = "`shared/vinca.yaml`"
    else:
        for distro, add in plan.items():
            _append_selection(rs.DISTROS / distro / "vinca.yaml", add)
            rs.task(distro, "sort", [])
        where = ", ".join(f"`distros/{d}/vinca.yaml`" for d in plan)

    # what would be built (linux-64; packages already on the channel are skipped)
    if lines:
        lines.append("")
    builds_ok = True
    for distro, add in plan.items():
        rc = rs.task(distro, "generate-recipes", ["--platform", "linux-64"])
        recipes = sorted(p.name for p in (rs.work_dir(distro) / "recipes").iterdir()) if not rc else []
        new = [r for r in recipes if r.startswith("ros2-") or not r.startswith("ros-")]
        if rc:
            builds_ok = False
            lines.append(f"- **{distro}**: adding {', '.join(f'`{p}`' for p in add)} — recipe generation **failed**, see the log")
        else:
            shown = ", ".join(f"`{r}`" for r in new[:25]) + (f" and {len(new) - 25} more" if len(new) > 25 else "")
            published = [p for p in add if f"ros2-{p.replace('_', '-')}" not in new]
            lines.append(f"- **{distro}**: adding {', '.join(f'`{p}`' for p in add)} → {len(new)} packages to build on linux-64"
                         + (f": {shown}" if new else ""))
            if published:
                lines.append(f"  - already published (built as a dependency): {', '.join(f'`{p}`' for p in published)}")
    if preview:
        for path, content in backup.items():
            path.write_bytes(content)
        request = " ".join(wanted + (distros if distros else []))
        lines += ["", f"_Preview only: a maintainer can open the PR with `@robostack-bot add-package {request}`._"]
        return Result(f"add-package {' '.join(wanted)} (preview)", "\n".join(lines), ok=builds_ok)
    lines += ["", f"Added to {where}. CI builds the new packages on all platforms."]
    return Result(f"Add {', '.join(wanted)}", "\n".join(lines), changed=True, ok=builds_ok)


def _default_ci_yaml() -> str:
    return (rs.TOOLS / "ci.default.yaml").read_text()


def parse_command(body: str, association: str) -> list[dict]:
    """What a comment or issue asks for:

    - `@robostack-bot <command> [<distro>... | all]`, `@robostack-bot add-package <pkg>... [<distro>...]`
    - the "robostack-bot command" and "Package request" issue forms

    Returns the jobs to run, each {"command", "distro", "args", "preview"} (one per
    distribution for the per-distribution commands). Anyone can request packages
    (add-package opens the PR for them too, maintainers review and merge it); the
    other commands are for owners, members and collaborators.
    """
    body = body or ""
    maintainer = association.upper() in ALLOWED_ASSOCIATIONS
    command, rest = "", ""
    if m := re.search(r"^\s*@robostack-bot,?\s+(?:please\s+)?([a-z-]+)([^\n]*)", body, re.I | re.M):
        command, rest = m.group(1).lower(), m.group(2)
    else:
        form = _form_fields(body)
        if form.get("package names"):
            command, rest = "add-package", form["package names"]
        elif form.get("command"):
            command, rest = form["command"].lower(), form.get("packages", "")
        rest += " " + form.get("distributions", "")
    if command not in BOT_COMMANDS:
        return []
    known = rs.distros()
    words = [w for w in re.split(r"[\s,]+", rest.strip()) if w and w.lower() not in ("_no", "response_", "none")]
    distros = [w.lower() for w in words if w.lower() in known]
    if any(w.lower() == "all" for w in words):
        distros = []  # every distribution
    if command == "add-package":
        packages = [w for w in words if w.lower() not in known and w.lower() != "all"]
        packages = [p for p in packages if PACKAGE_NAME.match(_package_name(p))][:MAX_PACKAGES]
        if not packages:
            return []
        return [{"command": command, "distro": "", "args": " ".join(packages + distros), "preview": False}]
    if not maintainer:
        return []
    if command in PER_DISTRO_COMMANDS:
        return [{"command": command, "distro": d, "args": "", "preview": False} for d in distros or known]
    return [{"command": command, "distro": "", "args": "", "preview": False}]


def _form_fields(body: str) -> dict[str, str]:
    """Fields of an issue form: `### Label` headings, each followed by its value."""
    fields: dict[str, str] = {}
    for m in re.finditer(r"(?m)^###\s*(.+?)\s*\n([\s\S]*?)(?=^###|\Z)", body):
        label, value = m.group(1).strip().lower(), m.group(2).strip()
        label = {"package name": "package names", "distribution": "distributions"}.get(label, label)
        checked = re.findall(r"- \[[xX]\]\s*(\S+)", value)
        fields[label] = " ".join(checked) if re.search(r"- \[[ xX]\]", value) else value
    return fields


def check() -> Result:
    """Sanity checks: every distribution assembles and its generated files are consistent."""
    problems: list[str] = []
    known = {"channel_name", "upload_target", "conda_forge_pinning_version", "conda_forge_migrations", "pinning_overrides"}
    for distro in rs.distros():
        d = rs.DISTROS / distro
        settings = rs.settings(distro)
        if unknown := set(settings) - known:
            problems.append(f"{distro}: unknown keys in distro.yaml: {sorted(unknown)}")
        if settings.get("upload_target", "prefix") not in ("prefix", "anaconda"):
            problems.append(f"{distro}: upload_target must be prefix or anaconda")
        if bool(settings.get("conda_forge_pinning_version")) != bool(settings.get("conda_forge_migrations")):
            problems.append(f"{distro}: set conda_forge_pinning_version and conda_forge_migrations together")
        w = rs.prepare(distro)
        vinca = yaml.safe_load((w / "vinca.yaml").read_text())
        if vinca.get("ros_distro") != distro:
            problems.append(f"{distro}: vinca.yaml has ros_distro {vinca.get('ros_distro')!r}")
        for key in ("build_number", "mutex_package", "packages_select_by_deps"):
            if not vinca.get(key):
                problems.append(f"{distro}: vinca.yaml has no {key}")
        for patch in (d / "patch").glob("*.patch"):
            if not re.match(rf"^(ros-{distro}-|ros2-)?[a-z0-9-]+(\.(osx|linux|win|unix|emscripten))?\.patch$", patch.name):
                problems.append(f"{distro}: unexpected patch name {patch.name}")
            elif patch.name.startswith("ros-") and not patch.name.startswith(f"ros-{distro}-"):
                problems.append(f"{distro}: patch {patch.name} names another distribution")
        rendered = d / "conda_build_config.yaml"
        before = rendered.read_text() if rendered.is_file() else ""
        if rs.task(distro, "render-pinning", []) or rendered.read_text() != before:
            problems.append(f"{distro}: conda_build_config.yaml is out of date (pixi run rs {distro} render-pinning)")
            rendered.write_text(before)
    for wf in sorted((rs.ROOT / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text()
        if "\non:" not in "\n" + text or "\ntrue:" in "\n" + text:
            problems.append(f"{wf.name}: no top-level `on:` trigger")
    sort = subprocess.run(["pixi", "run", "sort", "--check"], cwd=rs.ROOT, capture_output=True, text=True)
    if sort.returncode:
        problems.append("YAML files are not sorted (pixi run sort):\n" + tail(sort.stdout + sort.stderr, 10))
    ok = not problems
    summary = "All distributions assemble; generated files are up to date." if ok else "\n".join(f"- {p}" for p in problems)
    return Result("check: ok" if ok else "check: problems found", summary, ok=ok)


# --------------------------------------------------------------------------- #
def report(result: Result, summary_file: str | None) -> int:
    print(f"# {result.title}\n\n{result.summary}")
    if summary_file:
        Path(summary_file).write_text(result.summary + "\n\n🤖 robostack-bot\n")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as fh:
            fh.write(f"title={result.title}\nchanged={str(result.changed).lower()}\nok={str(result.ok).lower()}\n")
    return 0 if result.ok else 1


def main(command: str, argv: list[str], distro: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"rs {command}")
    parser.add_argument("--summary")
    if command == "new-distro":
        parser.add_argument("name")
        parser.add_argument("--from", dest="source", required=True)
    if command == "parse-command":
        parser.add_argument("--body", required=True)
        parser.add_argument("--association", required=True)
    if command == "add-package":
        parser.add_argument("names", nargs="+", help="ROS packages, optionally followed by distributions")
        parser.add_argument("--preview", action="store_true", help="don't change files, only report")
    args = parser.parse_args(argv)
    if command == "parse-command":
        print(json.dumps(parse_command(args.body, args.association)))
        return 0
    if command == "add-package":
        known = set(rs.distros())
        distros = [n for n in args.names if n in known]
        packages = [n for n in args.names if n not in known]
        return report(add_package(packages, distros, preview=args.preview), args.summary)
    if command == "check":
        return report(check(), args.summary)
    if command == "update-pinning":
        return report(update_pinning(), args.summary)
    if command == "new-distro":
        return report(new_distro(args.name, args.source), args.summary)
    if command == "update-snapshot":
        return report(update_snapshot(distro), args.summary)
    if command == "find-stale":
        report(find_stale(distro), args.summary)
        return 0  # findings, not a failure
    raise SystemExit(f"unknown command {command}")

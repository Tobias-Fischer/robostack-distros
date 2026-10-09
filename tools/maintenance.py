"""Maintenance commands (used by robostack-bot, .github/workflows/bot.yaml).

    pixi run rs check                                 # sanity checks of the whole repository
    pixi run rs <distro> update-snapshot              # snapshot the latest rosdistro sync, summarise bumps
    pixi run rs rosdistro-syncs                       # bot jobs for distributions with a new sync
    pixi run rs <distro> find-stale                   # published packages built against outdated pins
    pixi run rs <distro> update-pinning               # latest conda-forge pinning + rebuild plan
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
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import robostack as rs

BOT_COMMANDS = {
    "add-package": "add ROS packages to the package selection (opens a PR; anyone can ask)",
    "update-rosdistro-snapshot": "refresh the distribution's rosdistro_snapshot.yaml (opens a PR)",
    "find-stale-packages": "list its published packages built against outdated pins",
    "update-conda-forge-pinning": "move the conda-forge pinning to the latest version, rebuilding what changed pins affect (opens a PR)",
}
PER_DISTRO_COMMANDS = ("update-rosdistro-snapshot", "find-stale-packages", "update-conda-forge-pinning")
ALLOWED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass
class Result:
    title: str
    summary: str
    changed: bool = False
    ok: bool = True
    labels: list[str] = field(default_factory=list)  # extra labels for the bot's PR


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
    the same package in conda_build_config.yaml; a shared constraint that no longer
    matches gets an override in the distribution's vinca.yaml. Other constraints stay
    as they are."""
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
        text = head + sep + block_new + rest[len(block):]

    # constraints from shared/vinca.yaml that the distribution doesn't override:
    # an outdated one gets an override in the distribution's file
    own = (yaml.safe_load(text).get("mutex_package") or {}).get("run_constraints") or []
    own_names = {rs._constraint_name(c) for c in own}
    shared = (rs.load_yaml(rs.SHARED / "vinca.yaml").get("mutex_package") or {}).get("run_constraints") or []
    overrides = []
    for constraint in shared:
        m = re.fullmatch(r"([A-Za-z0-9_.-]+) ([0-9][0-9.]*)\.\*", str(constraint).strip())
        if not m or m.group(1) in own_names:
            continue
        line = repl(re.match(r"()(.*) (.*)", f"{m.group(1)} {m.group(2)}"))
        if line != f"{m.group(1)} {m.group(2)}":
            overrides.append(line)
    if overrides:
        lines = "".join(f"    - {o}\n" for o in overrides)
        if sep:
            text = re.sub(r"(?m)^(  run_constraints:\n)", lambda mm: mm.group(1) + lines, text, count=1)
        else:
            text = re.sub(r"(?m)^(mutex_package:\n(?:[ \t]+.*\n)*)", lambda mm: mm.group(1) + "  run_constraints:\n" + lines, text, count=1)
    path.write_text(text)
    return changes


REPODATA_SUBDIRS = ("noarch", "linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64")


def released_builds(distro: str) -> dict[str, int] | None:
    """Highest build number of every package on the distribution's channel (all
    platforms), or None when the channel can't be read."""
    import urllib.error
    import urllib.request

    s = rs.settings(distro)
    name = s.get("channel_name", f"robostack-{distro}")
    base = f"https://repo.prefix.dev/{name}" if s.get("upload_target", "prefix") == "prefix" else rs.channel_url(distro)
    builds: dict[str, int] = {}
    read = False
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
            builds[record["name"]] = max(builds.get(record["name"], 0), int(record.get("build_number", 0)))
    return builds if read else None


def released_build_number(distro: str, builds: dict[str, int] | None = None) -> int | None:
    """Highest build number of the distribution's packages on its channel (the mutex
    aside), or None when the channel can't be read."""
    builds = released_builds(distro) if builds is None else builds
    if builds is None:
        return None
    mutex = (rs.read_vinca(distro).get("mutex_package") or {}).get("name")
    prefixes = (f"ros-{distro}-", "ros2-")
    numbers = [n for name, n in builds.items() if name.startswith(prefixes) and name != mutex]
    return max(numbers) if numbers else None


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


# A pinning change that rebuilds more than this share of a distribution's packages is
# done as a full rebuild (new mutex minor version) instead of a partial one.
FULL_REBUILD_SHARE = 0.5


def plan_rebuild(distro: str, old_cbc: str) -> tuple[dict[str, str], int]:
    """Packages of the distribution that the change from old_cbc to its current
    conda_build_config.yaml has to rebuild (with the reason), and how many packages
    it has (vinca-rebuild-plan)."""
    from vinca import rebuild

    new = yaml.safe_load((rs.DISTROS / distro / "conda_build_config.yaml").read_text()) or {}
    changed = rebuild.changed_pins(yaml.safe_load(old_cbc) or {}, new)
    if not changed:
        return {}, 0
    requirements, prefix = rebuild.requirements_from_vinca(rs.prepare(distro))
    return rebuild.plan(requirements, changed, prefix), len(requirements)


def bump_partial(distro: str, packages: dict[str, str], mutex_changed: bool) -> list[str]:
    """Rebuild just these packages: a per-package build number above everything the
    channel has, and, when its run_constraints changed, a new build of the mutex (its
    version stays, so every other published package keeps working with it)."""
    builds = released_builds(distro)
    current = int(re.search(r"(?m)^build_number:\s*(\d+)", (rs.DISTROS / distro / "vinca.yaml").read_text()).group(1))
    released = released_build_number(distro, builds)
    number = (current if released is None else max(current, released)) + 1

    info = rs.DISTROS / distro / "pkg_additional_info.yaml"
    ry = _ruamel()
    data = (ry.load(info.read_text()) if info.is_file() else None) or {}
    for pkg in packages:
        entry = data.get(pkg)
        if entry is None:
            data[pkg] = {"build_number": number}
        else:
            entry["build_number"] = number
    with info.open("w") as fh:
        ry.dump(data, fh)
    lines = [f"{len(packages)} packages get `build_number: {number}` in `distros/{distro}/pkg_additional_info.yaml`"]

    if mutex_changed:
        path = rs.DISTROS / distro / "vinca.yaml"
        text = path.read_text()
        mutex = (rs.read_vinca(distro).get("mutex_package") or {}).get("name")
        mutex_build = ((builds or {}).get(mutex, number - 1)) + 1
        block = re.search(r"(?m)^mutex_package:\n((?:[ \t]+.*\n|\n)*)", text)
        if not block:
            raise SystemExit(f"{path}: no mutex_package")
        body = block.group(1)
        if re.search(r"(?m)^  build_number:", body):
            body = re.sub(r"(?m)^(  build_number:\s*)\d+", rf"\g<1>{mutex_build}", body, count=1)
        else:
            body = f"  build_number: {mutex_build}\n" + body
        path.write_text(text[: block.start(1)] + body + text[block.end(1):])
        lines.append(f"mutex `{mutex}`: new build {mutex_build} with the updated run_constraints (same version)")
    return lines


def dependency_conflicts(distro: str) -> set[str] | None:
    """Packages that can't be installed together with the rest of the distribution's
    dependencies under its current pins and mutex constraints (check_dependency_compat.py,
    linux-64); None when the check itself failed."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "conflicts.json"
        rc = rs.task(distro, "check-deps", ["--platform", "linux-64", "--no-migrations", "--json", str(out)])
        if rc == 0:
            return set()
        if not out.is_file():
            return None
        data = json.loads(out.read_text())
    return set(data.get("pin_conflicts") or {}) | set(data.get("conflicts") or {})


def _details(summary: str, lines: list[str]) -> list[str]:
    return ["<details>", f"<summary>{summary}</summary>", "", *lines, "", "</details>"]


SYNC_TAG = re.compile(r"^(?P<distro>[a-z]+)/(?P<date>\d{4}-\d{2}-\d{2})$")
ROSDISTRO = "https://github.com/ros/rosdistro"


def latest_sync(distro: str) -> str | None:
    """Newest sync tag of the distribution in ros/rosdistro, e.g. jazzy/2026-10-05.
    The ROS release team tags every sync from ros-testing to the main repositories."""
    import urllib.request

    headers = {"User-Agent": "robostack-bot", "Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/ros/rosdistro/git/matching-refs/tags/{distro}/"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        refs = json.load(response)
    tags = [r["ref"].removeprefix("refs/tags/") for r in refs]
    tags = [t for t in tags if (m := SYNC_TAG.match(t)) and m["distro"] == distro]
    return max(tags, key=lambda t: t.split("/")[1], default=None)


def _announcement(distro: str, date: str) -> str:
    """Link to the Discourse announcement of a sync ("New Packages for Jazzy Jalisco
    2026-10-05"), or to a search for it."""
    import urllib.parse
    import urllib.request

    search = f"https://discourse.openrobotics.org/search?q={urllib.parse.quote(f'{distro} {date} in:title')}"
    try:
        request = urllib.request.Request(search.replace("/search?", "/search.json?"), headers={"User-Agent": "robostack-bot"})
        with urllib.request.urlopen(request, timeout=30) as response:
            topics = json.load(response).get("topics", [])
    except Exception:
        return search
    for topic in topics:
        if re.match(r"(?i)new packages", topic.get("title", "")) and date in topic["title"]:
            return f"https://discourse.openrobotics.org/t/{topic['slug']}/{topic['id']}"
    return search


def _set_setting(distro: str, key: str, value: str) -> None:
    """Set a top-level scalar in distros/<d>/distro.yaml, keeping the rest as is."""
    path = rs.DISTROS / distro / "distro.yaml"
    text = path.read_text()
    line = f"{key}: {value}"
    if re.search(rf"(?m)^{key}:.*$", text):
        text = re.sub(rf"(?m)^{key}:.*$", line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text)


def _pending_sync(distro: str) -> str | None:
    """The sync recorded on an open bot PR for the distribution, if any."""
    branch = f"bot/update-rosdistro-snapshot-{distro}"
    fetch = subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", branch], cwd=rs.ROOT, capture_output=True)
    if fetch.returncode:
        return None
    show = subprocess.run(["git", "show", f"FETCH_HEAD:distros/{distro}/distro.yaml"], cwd=rs.ROOT,
                          capture_output=True, text=True)
    return (yaml.safe_load(show.stdout) or {}).get("rosdistro_sync") if show.returncode == 0 else None


def rosdistro_syncs() -> list[dict]:
    """Bot jobs for the distributions with a rosdistro sync newer than the one they
    (or their open snapshot PR) are on. Distributions with `rosdistro_sync: manual`
    are left out."""
    jobs = []
    for distro in rs.distros():
        recorded = rs.settings(distro).get("rosdistro_sync")
        if recorded == "manual":
            continue
        latest = latest_sync(distro)
        known = [t for t in (recorded, _pending_sync(distro)) if t]
        if latest and all(latest.split("/")[1] > t.split("/")[1] for t in known):
            print(f"{distro}: new rosdistro sync {latest} (on {recorded or 'none'})", file=sys.stderr)
            jobs.append({"command": "update-rosdistro-snapshot", "distro": distro, "args": "", "preview": False})
        else:
            print(f"{distro}: on the latest sync {latest}", file=sys.stderr)
    return jobs


def update_snapshot(distro: str) -> Result:
    """Snapshot the distribution's latest rosdistro sync (rosdistro master with
    `rosdistro_sync: manual`), then bump for a full rebuild."""
    snapshot = rs.DISTROS / distro / "rosdistro_snapshot.yaml"
    recorded = rs.settings(distro).get("rosdistro_sync")
    ref = None if recorded == "manual" else latest_sync(distro)
    old = _versions(snapshot)
    if rs.task(distro, "create-snapshot", ["--rosdistro-ref", ref] if ref else []):
        return Result(f"{distro}: snapshot update failed", "`vinca-snapshot` failed, see the log.", ok=False)
    new = _versions(snapshot)
    if ref:
        _set_setting(distro, "rosdistro_sync", ref)
        date = ref.split("/")[1]
        source = [
            f"rosdistro sync [`{ref}`]({ROSDISTRO}/tree/{ref})"
            + (f" ([changes since `{recorded}`]({ROSDISTRO}/compare/{recorded}...{ref}))" if recorded else "")
            + f", [announcement]({_announcement(distro, date)}).",
            "",
        ]
        title = f"{distro}: rosdistro sync {date}"
    else:
        source, title = ["rosdistro master (this distribution doesn't follow syncs).", ""], f"{distro}: update rosdistro snapshot"
    if old == new:
        return Result(f"{distro}: snapshot is up to date", "\n".join(source) + "No package versions changed.",
                      changed=bool(ref and ref != recorded))
    bump = bump_rebuild(distro)
    return Result(title, "\n".join(source) + snapshot_changes(old, new) + "\n\n" + "\n".join(f"- {l}" for l in bump),
                  changed=True)


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
def _set_pinning(distro: str, version: str, migrations: list[str]) -> str:
    """Write the new pinning version and migrations where the distribution takes them
    from: its own distro.yaml, or shared/pinning/conda_forge.yaml. Returns that file."""
    if rs.settings(distro).get("conda_forge_pinning_version"):
        path = rs.DISTROS / distro / "distro.yaml"
        text = path.read_text()
        text = re.sub(r"(?m)^conda_forge_pinning_version:.*$", f"conda_forge_pinning_version: {version}", text, count=1)
        text = re.sub(r"(?m)^conda_forge_migrations:(?:.*\n)(?:[ \t]+.*\n)*",
                      f"conda_forge_migrations: [{', '.join(migrations)}]\n", text, count=1)
        path.write_text(text)
        return f"distros/{distro}/distro.yaml"
    shared = rs.SHARED / "pinning" / "conda_forge.yaml"
    header = [l for l in shared.read_text().splitlines() if l.startswith("#")]
    body = [f"conda_forge_pinning_version: {version}", "migrations:"] + [f"  - {m}" for m in migrations]
    shared.write_text("\n".join(header + body) + "\n")
    return "shared/pinning/conda_forge.yaml"


def update_pinning(distro: str) -> Result:
    """Move the distribution's conda-forge pinning to the latest version, with the
    migrations that are done for its dependencies. Only the packages that use a changed
    pin (and what depends on them) are rebuilt, unless that is most of them."""
    from vinca import pinning

    settings = rs.settings(distro)
    before = (settings.get("conda_forge_pinning_version"), list(settings.get("conda_forge_migrations") or []))
    if not before[0]:
        shared = yaml.safe_load((rs.SHARED / "pinning" / "conda_forge.yaml").read_text()) or {}
        before = (shared.get("conda_forge_pinning_version"), list(shared.get("migrations") or []))
    print(f"Collecting dependencies of {distro}", flush=True)
    work = rs.prepare(distro)
    dependencies = pinning.dependencies_from_vinca(work, pinning.DEFAULT_PLATFORMS)
    version, migrations, reports = pinning.update_pinning(work / "vinca_pinning.yaml", dependencies=dependencies)
    if str(before[0]) == str(version) and before[1] == list(migrations):
        return Result(f"{distro}: pinning is up to date", f"{distro} is on conda-forge-pinning {version}.")
    where = _set_pinning(distro, str(version), list(migrations))

    cbc = rs.DISTROS / distro / "conda_build_config.yaml"
    before_cbc = cbc.read_text() if cbc.is_file() else ""
    print(f"Checking the dependencies of {distro} with the current pins", flush=True)
    conflicts_before = dependency_conflicts(distro)
    rs.task(distro, "render-pinning", [])
    added = sorted(set(migrations) - set(before[1]))
    candidates = {name for name, _ in reports}
    finished = sorted(set(before[1]) - set(migrations) - candidates)
    removed = sorted(set(before[1]) - set(migrations) - set(finished))
    lines = [
        f"Moves **{distro}** from conda-forge-pinning `{before[0]}` to `{version}` (`{where}`).",
        "",
        "Migrations: " + (", ".join(f"`{m}`" for m in migrations) or "none")
        + (f"; new: {', '.join(f'`{m}`' for m in added)}" if added else "")
        + (f"; finished (now part of the base pinning): {', '.join(f'`{m}`' for m in finished)}" if finished else "")
        + (f"; dropped: {', '.join(f'`{m}`' for m in removed)}" if removed else ""),
        "",
    ]
    conflicts = False
    if cbc.read_text() == before_cbc:
        lines.append("The rendered pins don't change: nothing to rebuild.")
    else:
        constraints = sync_mutex_constraints(distro)
        print(f"Planning the rebuild of {distro}", flush=True)
        packages, total = plan_rebuild(distro, before_cbc)
        if not packages and not constraints:
            lines.append("The pins change, but no package uses them: nothing to rebuild.")
        elif total and len(packages) > FULL_REBUILD_SHARE * total:
            lines.append(f"**Full rebuild**: {len(packages)} of {total} packages use a changed pin or depend "
                         "on one. " + "; ".join(bump_rebuild(distro)))
        else:
            lines.append(f"**Rebuild {len(packages)} of {total} packages** (they use a changed pin or depend "
                         "on one): " + "; ".join(bump_partial(distro, packages, bool(constraints))))
            if packages:
                lines += ["", *_details(f"Packages to rebuild ({len(packages)})", [
                    f"- `{name}`: {reason}" for name, reason in sorted(packages.items())])]
        if constraints:
            lines += ["", "Mutex run_constraints: " + ", ".join(constraints)]
        conflicts_after = dependency_conflicts(distro)
        if conflicts_after is None or conflicts_before is None:
            conflicts = conflicts_after is None
            lines += ["", "Dependency check (linux-64): ❌ the check failed, see the workflow log"
                      if conflicts else "Dependency check (linux-64): no comparison (the check with the old pins failed)"]
        else:
            new_conflicts = sorted(conflicts_after - conflicts_before)
            conflicts = bool(new_conflicts)
            lines += ["", "Dependency check (linux-64): " + (
                "❌ new conflicts: " + ", ".join(f"`{n}`" for n in new_conflicts) if new_conflicts
                else "✅ no new conflicts")]
            if conflicts_before:
                lines += ["", *_details(f"Conflicts that already exist with the current pins ({len(conflicts_before)})",
                                        [", ".join(f"`{n}`" for n in sorted(conflicts_before))])]

    groups: dict[str, list[str]] = {"selected": [], "waiting": [], "other": []}
    for name, text in reports:
        key = "selected" if text.startswith(("selected", "kept")) else "waiting" if text.startswith("waiting") else "other"
        groups[key].append(f"- `{name}`: {text}")
    lines += [
        "",
        *_details(f"Migrations used ({len(groups['selected'])})", groups["selected"] or ["none"]),
        "",
        *_details(f"Migrations waiting for our dependencies' feedstocks ({len(groups['waiting'])})",
                  groups["waiting"] or ["none"]),
        "",
        *_details(f"Migrations that don't concern {distro} ({len(groups['other'])})", groups["other"] or ["none"]),
    ]
    return Result(f"{distro}: update conda-forge pinning to {version}", "\n".join(lines), changed=True,
                  labels=["dependency-conflict"] if conflicts else [])


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


PLATFORMS = ("linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64")


def _selected(distro: str) -> set[str]:
    """Packages the distribution selects (on at least one platform)."""
    names: set[str] = set()
    for platform in PLATFORMS:
        names |= {n.replace("-", "_") for n in rs.read_vinca(distro, platform)["packages_select_by_deps"]}
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
    """Select ROS packages for building in shared/vinca.yaml, and list for the given
    (default: all) distributions the recipes this adds on linux-64."""
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

    def recipes(distro: str) -> set[str] | None:
        """Recipes to build on linux-64 (packages already on the channel are skipped)."""
        if rs.task(distro, "generate-recipes", ["--platform", "linux-64"]):
            return None
        return {p.name for p in (rs.work_dir(distro) / "recipes").iterdir()}

    before = {distro: recipes(distro) for distro in plan}

    # the whole selection is shared; vinca skips packages a distribution doesn't have
    backup = {p: p.read_bytes() for p in [rs.SHARED / "vinca.yaml"]}
    _append_selection(rs.SHARED / "vinca.yaml", sorted({pkg for add in plan.values() for pkg in add}))
    subprocess.run(["vinca-sort-vinca-lists", "shared/vinca.yaml"], cwd=rs.ROOT, check=True)
    where = "`shared/vinca.yaml` (every distribution that has them builds them)"

    # what the request adds to the build (linux-64)
    if lines:
        lines.append("")
    builds_ok = True
    for distro, add in plan.items():
        after = recipes(distro)
        if after is None or before[distro] is None:
            builds_ok = False
            lines.append(f"- **{distro}**: adding {', '.join(f'`{p}`' for p in add)} — recipe generation **failed**, see the log")
            continue
        new = sorted(after - before[distro])
        shown = ", ".join(f"`{r}`" for r in new[:25]) + (f" and {len(new) - 25} more" if len(new) > 25 else "")
        published = [p for p in add if f"ros2-{p.replace('_', '-')}" not in after]
        lines.append(f"- **{distro}**: adding {', '.join(f'`{p}`' for p in add)} → {len(new)} new package{'s' if len(new) != 1 else ''} to build on linux-64"
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
    return (rs.TOOLS / "ci_default.yaml").read_text()


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
    known = {"channel_name", "upload_target", "conda_forge_pinning_version", "conda_forge_migrations", "pinning_overrides",
             "rosdistro_sync"}
    for distro in rs.distros():
        d = rs.DISTROS / distro
        settings = rs.settings(distro)
        if unknown := set(settings) - known:
            problems.append(f"{distro}: unknown keys in distro.yaml: {sorted(unknown)}")
        if settings.get("upload_target", "prefix") not in ("prefix", "anaconda"):
            problems.append(f"{distro}: upload_target must be prefix or anaconda")
        sync = settings.get("rosdistro_sync")
        if sync is not None and sync != "manual" and not ((m := SYNC_TAG.match(str(sync))) and m["distro"] == distro):
            problems.append(f"{distro}: rosdistro_sync must be 'manual' or a sync tag like {distro}/2026-10-05")
        if bool(settings.get("conda_forge_pinning_version")) != bool(settings.get("conda_forge_migrations")):
            problems.append(f"{distro}: set conda_forge_pinning_version and conda_forge_migrations together")
        try:
            vinca = rs.read_vinca(distro, "linux-64")
        except Exception as error:  # noqa: BLE001 - reported as a problem
            problems.append(f"{distro}: vinca can't read its configuration: {error}")
            continue
        if vinca.get("ros_distro") != distro:
            problems.append(f"{distro}: vinca.yaml has ros_distro {vinca.get('ros_distro')!r}")
        for key in ("build_number", "mutex_package", "packages_select_by_deps"):
            if not vinca.get(key):
                problems.append(f"{distro}: vinca.yaml has no {key}")
        for patch in (d / "patch").glob("*.patch"):
            if not re.match(r"^ros2-[a-z0-9-]+(\.(osx|linux|win|unix|emscripten))?\.patch$", patch.name):
                problems.append(f"{distro}: patch {patch.name} should be named ros2-<package>[.<platform>].patch")
        rendered = d / "conda_build_config.yaml"
        before = rendered.read_text() if rendered.is_file() else ""
        if rs.task(distro, "render-pinning", []) or rendered.read_text() != before:
            problems.append(f"{distro}: conda_build_config.yaml is out of date (pixi run rs {distro} render-pinning)")
            rendered.write_text(before)
    # file names: .yaml everywhere (GitHub requires FUNDING.yml by that name)
    for yml in sorted(p for p in (rs.ROOT / ".github").rglob("*.yml") if p.name != "FUNDING.yml"):
        problems.append(f"{yml.relative_to(rs.ROOT)}: use the .yaml extension")
    for wf in sorted((rs.ROOT / ".github" / "workflows").glob("*.yaml")):
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
            fh.write(f"labels={','.join(result.labels)}\n")
    return 0 if result.ok else 1


def main(command: str, argv: list[str], distro: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"rs {command}")
    parser.add_argument("--summary")
    if command == "new-distro":
        parser.add_argument("name")
        parser.add_argument("--from", dest="source", required=True)
    if command == "parse-command":
        # the text comes from the environment: pixi's task shell would re-parse quotes in it
        parser.add_argument("--body", default=os.environ.get("ROBOSTACK_BOT_BODY", ""))
        parser.add_argument("--association", default=os.environ.get("ROBOSTACK_BOT_ASSOCIATION", ""))
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
    if command == "rosdistro-syncs":
        print(json.dumps(rosdistro_syncs()))
        return 0
    if command == "update-pinning":
        if distro:
            return report(update_pinning(distro), args.summary)
        results = [update_pinning(d) for d in rs.distros()]
        return report(Result("Update conda-forge pinning", "\n\n---\n\n".join(r.summary for r in results),
                             changed=any(r.changed for r in results), ok=all(r.ok for r in results),
                             labels=sorted({label for r in results for label in r.labels})), args.summary)
    if command == "new-distro":
        return report(new_distro(args.name, args.source), args.summary)
    if command == "update-snapshot":
        return report(update_snapshot(distro), args.summary)
    if command == "find-stale":
        report(find_stale(distro), args.summary)
        return 0  # findings, not a failure
    raise SystemExit(f"unknown command {command}")

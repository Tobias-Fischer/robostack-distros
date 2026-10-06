"""Maintenance commands (used by robostack-bot, .github/workflows/bot.yml).

    pixi run rs check                                 # sanity checks of the whole repository
    pixi run rs <distro> update-snapshot              # refresh rosdistro_snapshot.yaml, summarise bumps
    pixi run rs <distro> find-stale                   # published packages built against outdated pins
    pixi run rs update-pinning                        # move shared/pinning/conda_forge.yaml forward
    pixi run rs new-distro NAME --from DISTRO         # add distros/NAME, seeded from DISTRO
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
    added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
    bumped = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    lines = [f"{len(bumped)} updated, {len(added)} added, {len(removed)} removed packages."]
    if bumped:
        lines += ["", "| package | old | new |", "|---|---|---|"] + [f"| {k} | {old[k]} | {new[k]} |" for k in bumped]
    if added:
        lines += ["", "Added: " + ", ".join(f"`{k}`" for k in added)]
    if removed:
        lines += ["", "Removed: " + ", ".join(f"`{k}`" for k in removed)]
    return "\n".join(lines)


def update_snapshot(distro: str) -> Result:
    snapshot = rs.DISTROS / distro / "rosdistro_snapshot.yaml"
    old = _versions(snapshot)
    if rs.task(distro, "create-snapshot", []):
        return Result(f"{distro}: snapshot update failed", "`vinca-snapshot` failed, see the log.", ok=False)
    new = _versions(snapshot)
    if old == new:
        return Result(f"{distro}: snapshot is up to date", "No package versions changed.")
    return Result(f"{distro}: update rosdistro snapshot", snapshot_changes(old, new), changed=True)


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
        f"`check_dependency_compat.py --stale --repodata {url}`\n\n```\n{output}\n```",
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
    for distro in followers:
        rs.task(distro, "render-pinning", [])
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


def _default_ci_yaml() -> str:
    return (
        "# Temporary controls for the pull-request build (.github/workflows/testpr.yml).\n"
        "full_rebuild: false\n"
        "evict_cache: []\n"
    )


def parse_command(body: str, association: str) -> tuple[str, str] | None:
    """`@robostack-bot <command> [<distro>]` by a maintainer, or the command issue form."""
    if association.upper() not in ALLOWED_ASSOCIATIONS:
        return None
    m = re.search(r"^\s*@robostack-bot,?\s+(?:please\s+)?([a-z-]+)(?:\s+([a-z]+))?", body or "", re.I | re.M)
    if not m:
        m = re.search(r"###\s*Command\s*\n+\s*([a-z-]+)(?:[\s\S]*?###\s*Distribution\s*\n+\s*([a-z]+))?", body or "", re.I)
    if not m:
        return None
    command, distro = m.group(1).lower(), (m.group(2) or "").lower()
    if command not in BOT_COMMANDS:
        return None
    if command in PER_DISTRO_COMMANDS and distro not in rs.distros():
        return None
    return command, distro if command in PER_DISTRO_COMMANDS else ""


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
    args = parser.parse_args(argv)
    if command == "parse-command":
        parsed = parse_command(args.body, args.association)
        command_, distro_ = parsed or ("", "")
        print(json.dumps({"command": command_, "distro": distro_}))
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a") as fh:
                fh.write(f"command={command_}\ndistro={distro_}\n")
        return 0
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

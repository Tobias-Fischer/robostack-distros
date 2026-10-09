"""Freshness fences for the existing build-branch and public-upload workflows."""

import argparse
import io
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


class StaleRelease(RuntimeError):
    """The source or build branch has been superseded; do not push or upload."""


def relevant_path(path: str, distro: str) -> bool:
    """Match the shared/distro input scope used by changed-distros."""
    return (not path.endswith(".md") and path != "LICENSE"
            and (not path.startswith("distros/") or path.startswith(f"distros/{distro}/")))


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def branch_name(distro: str, platform: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", distro) or platform not in (
        "linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64"
    ):
        raise ValueError("invalid distribution or build platform")
    return f"buildbranch_{distro}_{platform.replace('-', '_')}"


def is_ancestor(root: Path, older: str, newer: str) -> bool:
    result = subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode())
    return result.returncode == 0


def refresh_source(root: Path = ROOT, remote: str = "origin") -> str:
    """A lock holder assumes its distro's latest work, even for a delayed event.

    GitHub may replace a pending concurrency run out of order. Refreshing after
    acquiring the distro lock makes that replacement own the latest inputs too.
    """
    git(root, "fetch", "--no-tags", remote, "+refs/heads/main:refs/robostack/main")
    latest = git(root, "rev-parse", "refs/robostack/main")
    git(root, "checkout", "--detach", latest)
    return latest


def assert_source_current(distro: str, source: str, root: Path = ROOT,
                          remote: str = "origin") -> None:
    git(root, "fetch", "--no-tags", remote, "+refs/heads/main:refs/robostack/main")
    latest = git(root, "rev-parse", "refs/robostack/main")
    if not is_ancestor(root, source, latest):
        raise StaleRelease(f"{source} is no longer on main")
    # A later merge for another distro must not discard this distro's pending work.
    changed = git(root, "diff", "--name-only", "-z", source, latest).split("\0")
    if any(path and relevant_path(path, distro) for path in changed):
        raise StaleRelease(f"{distro}: main has newer build inputs than {source}")


def remote_branch(root: Path, remote: str, branch: str) -> str:
    result = subprocess.run(["git", "ls-remote", "--exit-code", remote, f"refs/heads/{branch}"],
                            cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 2:
        return ""
    if result.returncode:
        raise RuntimeError(result.stderr)
    git(root, "fetch", "--no-tags", remote, f"+refs/heads/{branch}:refs/robostack/build")
    return git(root, "rev-parse", "refs/robostack/build")


def push_branch(distro: str, platform: str, source: str, root: Path = ROOT,
                remote: str = "origin") -> None:
    """Replace a build branch only from current inputs and with a compare-and-swap."""
    branch = branch_name(distro, platform)
    expected = remote_branch(root, remote, branch)
    if expected:
        # Every generated branch is exactly one generated commit atop its main source.
        previous_source = git(root, "rev-parse", f"{expected}^")
        if not is_ancestor(root, previous_source, source):
            raise StaleRelease(f"{branch} already has a newer or unrelated source")
    if git(root, "rev-parse", "HEAD^") != git(root, "rev-parse", source):
        raise RuntimeError("build branch must be one generated commit atop its source")
    assert_source_current(distro, source, root, remote)
    # An intervening push after the checks fails, including first-branch creation.
    git(root, "push", remote, f"HEAD:refs/heads/{branch}",
        f"--force-with-lease=refs/heads/{branch}:{expected}")


def assert_publishable(distro: str, root: Path = ROOT) -> None:
    """Local uploads keep their existing behavior; Actions uploads must be current."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    source = os.environ.get("ROBOSTACK_SOURCE_SHA", "")
    platform = os.environ.get("ROBOSTACK_PLATFORM", "")
    commit = os.environ.get("GITHUB_SHA", "")
    if not source or not platform or not commit:
        raise StaleRelease("Actions publication requires generated release source/platform metadata")
    branch = branch_name(distro, platform)
    if os.environ.get("GITHUB_REF") != f"refs/heads/{branch}":
        raise StaleRelease("publication is only allowed from its generated build branch")
    if git(root, "rev-parse", "HEAD") != commit:
        raise StaleRelease("checked-out commit does not match this workflow run")
    if git(root, "rev-parse", "HEAD^") != source:
        raise StaleRelease("generated source does not match the build commit's parent")
    if remote_branch(root, "origin", branch) != commit:
        raise StaleRelease(f"{branch} has superseded this build")
    assert_source_current(distro, source, root)


def configure_workflow(text: str, distro: str, platform: str) -> str:
    """Apply repository release policy without changing Vinca's staged job graph."""
    from ruamel.yaml import YAML

    branch_name(distro, platform)
    yaml = YAML()
    yaml.preserve_quotes = True
    workflow = yaml.load(text)
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        raise ValueError("Vinca did not generate a workflow with jobs")
    workflow["name"] = "build"
    workflow["run-name"] = f"{distro} {platform}"
    workflow["concurrency"] = {
        "group": f"publish-{distro}-{platform}",
        "cancel-in-progress": False,
        "queue": "max",
    }
    workflow["permissions"] = {"contents": "read"}
    workflow.setdefault("env", {}).update({
        "ROBOSTACK_DISTRO": distro,
        "ROBOSTACK_PLATFORM": platform,
        "ROBOSTACK_SOURCE_SHA": git(ROOT, "rev-parse", "HEAD"),
        "ROBOSTACK_UPLOAD_CHANNEL": "${{ vars.ROBOSTACK_UPLOAD_CHANNEL }}",
        "PREFIX_API_KEY": "${{ secrets.PREFIX_API_KEY }}",
        "PIXI_LOCKED": "true",
    })
    if platform == "win-64" and not any(
        "build-ci" in str(step.get("run", ""))
        for job in workflow["jobs"].values() for step in job.get("steps", [])
    ):
        raise ValueError("Vinca's Windows workflow must use the repository build-ci script")
    for job in workflow["jobs"].values():
        job.setdefault("permissions", {})["contents"] = "read"
        steps = job["steps"]
        checkout = None
        for index, step in enumerate(steps):
            action = step.get("uses", "")
            if action.startswith("actions/checkout@"):
                checkout = index
                step.setdefault("with", {}).update({"fetch-depth": 0, "persist-credentials": False})
            if action.startswith("prefix-dev/setup-pixi@"):
                options = step.setdefault("with", {})
                options.pop("frozen", None)
                options["locked"] = True
        if checkout is None:
            raise ValueError("generated build job has no checkout for the freshness fence")
        steps.insert(checkout + 1, {
            "name": "Reject superseded release before building",
            "shell": "bash",
            "run": f"python tools/release.py check-publish --distro {distro}",
        })
    result = io.StringIO()
    yaml.dump(workflow, result)
    return result.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("refresh-source", "check-source", "push", "check-publish"))
    parser.add_argument("--distro", required=True)
    parser.add_argument("--platform")
    parser.add_argument("--source", default=os.environ.get("GITHUB_SHA"))
    args = parser.parse_args()
    try:
        if args.command == "refresh-source":
            print(refresh_source())
        elif args.command == "check-publish":
            assert_publishable(args.distro)
        elif not args.source:
            parser.error("--source or GITHUB_SHA is required")
        elif args.command == "check-source":
            assert_source_current(args.distro, args.source)
        elif not args.platform:
            parser.error("push requires --platform")
        else:
            push_branch(args.distro, args.platform, args.source)
    except (StaleRelease, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Release refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

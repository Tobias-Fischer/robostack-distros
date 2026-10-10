"""robostack-bot: command parsing and the pure helpers of rebuild, rebuild-dependents,
update-vinca and dependency-report (no network)."""

import json

import pytest

import maintenance
import robostack as rs


def jobs(body, association="MEMBER", pull_request=""):
    return maintenance.parse_command(body, association, pull_request)


def test_rebuild_parsing():
    assert jobs("@robostack-bot rebuild ros-jazzy-rclcpp std-msgs jazzy --with-dependents") == [
        {"command": "rebuild", "distro": "", "args": "rclcpp std_msgs jazzy --with-dependents", "preview": False}]
    assert jobs("@robostack-bot rebuild rclcpp all")[0]["args"] == "rclcpp"
    assert jobs("@robostack-bot rebuild jazzy") == []  # no package
    assert jobs("@robostack-bot rebuild rclcpp", association="NONE") == []  # maintainers only


def test_rebuild_dependents_only_on_pull_requests():
    assert jobs("@robostack-bot rebuild-dependents", pull_request="44") == [
        {"command": "rebuild-dependents", "distro": "", "args": "44", "preview": False}]
    assert jobs("@robostack-bot rebuild-dependents") == []  # an issue, not a pull request
    assert jobs("@robostack-bot rebuild-dependents", association="CONTRIBUTOR", pull_request="44") == []


def test_update_vinca_parsing():
    assert jobs("@robostack-bot update-vinca")[0]["args"] == ""
    assert jobs("@robostack-bot update-vinca robostack-integration")[0]["args"] == "robostack-integration"
    assert jobs("@robostack-bot update-vinca $(rm -rf /)") == []


def test_dependency_report_is_per_distribution():
    assert [j["distro"] for j in jobs("@robostack-bot dependency-report all")] == rs.distros()
    assert jobs("@robostack-bot dependency-report jazzy") == [
        {"command": "dependency-report", "distro": "jazzy", "args": "", "preview": False}]


def test_rebuild_plan_follows_host_and_run_dependents():
    reqs = {
        "rclcpp": {"build": [["cmake"]], "host": [["ros2-rcl"]], "run": [[]]},
        "rcl": {"build": [[]], "host": [[]], "run": [[]]},
        "tf2": {"build": [[]], "host": [["ros2-rclcpp"]], "run": [[]]},
        "launch_py": {"build": [[]], "host": [[]], "run": [["ros2-tf2"]]},  # transitively, via run
        "unrelated": {"build": [[]], "host": [["ros2-rcl"]], "run": [[]]},
    }
    assert maintenance.rebuild_plan(reqs, "ros2", ["rclcpp", "missing"], False) == {"rclcpp": "requested"}
    assert maintenance.rebuild_plan(reqs, "ros2", ["rclcpp"], True) == {
        "rclcpp": "requested", "tf2": "depends on rclcpp", "launch_py": "depends on tf2"}


ABI_COMMENT = """<!-- robostack-abi-check -->
### ABI check (linux-64)

#### jazzy linux-64

| package | released → PR | verdict | details |
|---|---|---|---|
| rclcpp | 28.1.0 (build 7) → 28.2.0 | **soname**: ... | ... |

**To rebuild the dependents in this pull request**, add these to `distros/jazzy/pkg_additional_info.yaml` (...):

```yaml
tf2_ros:
  build_number: 9
rclcpp_action:
  build_number: 9
```

Compared with the newest builds on https://repo.prefix.dev/robostack-jazzy/linux-64.

#### humble linux-64

Nothing that needs a rebuild of other packages.

#### rolling linux-64

```yaml
tf2_ros:
  build_number: true
"bad name":
  build_number: 3
nav2_core:
  build_number: 4
```
"""


def test_abi_rebuilds_parses_the_snippets_per_distribution():
    assert maintenance.abi_rebuilds(ABI_COMMENT) == {
        "jazzy": {"tf2_ros": 9, "rclcpp_action": 9},
        "rolling": {"nav2_core": 4},  # invalid names and numbers are dropped
    }
    two_platforms = ABI_COMMENT.replace("#### humble linux-64", "#### jazzy linux-aarch64\n\n```yaml\ntf2_ros:\n  build_number: 11\n```")
    assert maintenance.abi_rebuilds(two_platforms)["jazzy"]["tf2_ros"] == 11


def test_merge_build_numbers_keeps_other_keys(tmp_path):
    info = tmp_path / "pkg_additional_info.yaml"
    info.write_text("tf2_ros:\n  additional_cmake_args: \"-DFOO=ON\"\nnav2_core:\n  build_number: 12\n")
    changed = maintenance.merge_build_numbers(info, {"tf2_ros": 9, "nav2_core": 9, "rclcpp_action": 9})
    assert changed == ["rclcpp_action", "tf2_ros"]  # nav2_core already has a higher number
    assert maintenance.yaml.safe_load(info.read_text()) == {
        "tf2_ros": {"additional_cmake_args": "-DFOO=ON", "build_number": 9},
        "nav2_core": {"build_number": 12}, "rclcpp_action": {"build_number": 9}}
    assert list(maintenance.yaml.safe_load(info.read_text())) == ["nav2_core", "rclcpp_action", "tf2_ros"]
    assert maintenance.merge_build_numbers(info, {"tf2_ros": 9}) == []


def test_rebuild_dependents_from_a_fork_replies_with_the_entries(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "RoboStack/robostack-distros")
    pull = {"state": "open", "head": {"ref": "main", "repo": {"full_name": "someone/robostack-distros"}}}

    def gh(args):
        if any(a.endswith("/comments") for a in args):
            return json.dumps(ABI_COMMENT) + "\n"
        return json.dumps(pull)

    monkeypatch.setattr(maintenance, "_gh", gh)
    result = maintenance.rebuild_dependents("44")
    assert not result.ok and "fork" in result.title
    assert "tf2_ros:\n  build_number: 9" in result.summary


PIXI = '''[pypi-dependencies]
# One vinca for all distributions.
vinca = { git = "https://github.com/Tobias-Fischer/vinca.git", rev = "cf6833a12f36509a90ea33d25766709bcc1eb2cf" }
# vinca = { path = "../vinca", editable = true }
'''


def test_vinca_pin():
    assert maintenance.vinca_pin(PIXI) == ("Tobias-Fischer/vinca", "cf6833a12f36509a90ea33d25766709bcc1eb2cf")
    updated = maintenance.set_vinca_pin(PIXI, "0123456789abcdef0123456789abcdef01234567")
    assert maintenance.vinca_pin(updated)[1] == "0123456789abcdef0123456789abcdef01234567"
    assert updated.replace("0123456789abcdef0123456789abcdef01234567", "cf6833a12f36509a90ea33d25766709bcc1eb2cf") == PIXI


def test_the_repository_pins_vinca():
    assert maintenance.vinca_pin((rs.ROOT / "pixi.toml").read_text())[0].endswith("/vinca")


def test_vinca_changes():
    compare = {"status": "ahead", "total_commits": 3, "commits": [
        {"sha": "a" * 40, "html_url": "https://github.com/o/vinca/commit/aaa", "commit": {"message": "Fix x\n\nmore"}},
        {"sha": "b" * 40, "html_url": "https://github.com/o/vinca/commit/bbb", "commit": {"message": "Add y"}}]}
    lines = maintenance.vinca_changes("o/vinca", "1" * 40, "2" * 40, compare)
    assert lines[0] == "[o/vinca `1111111111...2222222222`](https://github.com/o/vinca/compare/111111111111...222222222222)"
    assert lines[2:] == ["- [`aaaaaaa`](https://github.com/o/vinca/commit/aaa) Fix x",
                         "- [`bbbbbbb`](https://github.com/o/vinca/commit/bbb) Add y",
                         "- … and 1 more, see the link"]


CHECK_OUTPUT = """(58 solver runs)
conda-forge migration status (conda-forge-pinning 2026.09.01.16.28.00):
  visp  (feedstock: visp; pinned libs: ffmpeg)
    https://github.com/conda-forge/visp-feedstock

Legend: 'done' but still conflicting = ...
"""


def test_report_issue_body():
    data = {
        "pin_conflicts": {},
        "conflicts": {"visp": {"specs": ["visp >=3.7.0, <3.8.0a0"], "recipes": ["visp"], "pins": ["ffmpeg"],
                               "partners": [], "explanation": "visp 3.7.0 would require\n  ffmpeg >=7"}},
        "notes": {"libcamera": {"category": "runtime-glibc"}},
    }
    migrations = maintenance.migration_status(CHECK_OUTPUT)
    assert migrations.startswith("conda-forge migration status") and "Legend" not in migrations
    body = maintenance.report_issue_body("jazzy", data, migrations, "https://run")
    assert body.startswith(maintenance.REPORT_MARKER)
    assert "**1 conflict**" in body
    assert "| `visp` (visp >=3.7.0, <3.8.0a0) | 1: visp | `ffmpeg` |" in body
    assert "`libcamera`" in body and "visp-feedstock" in body and body.endswith("[Workflow run](https://run)")
    clean = maintenance.report_issue_body("jazzy", {}, "")
    assert "No conflicts" in clean and "|---|" not in clean


def test_publish_report(monkeypatch):
    calls = []
    existing = []

    def gh(args):
        calls.append(args)
        if "--jq" in args:
            return "".join(json.dumps(i) + "\n" for i in existing)
        return json.dumps({"html_url": "https://new"})

    monkeypatch.setattr(maintenance, "_gh", gh)
    # nothing to report and no issue: nothing happens
    assert maintenance.publish_report("o/r", "jazzy", "body", conflicts=False) is None
    assert len(calls) == 1
    # conflicts: a new issue
    assert maintenance.publish_report("o/r", "jazzy", "body", conflicts=True) == "https://new"
    assert "title=jazzy: dependency report" in calls[-1] and "labels[]=dependency-report" in calls[-1]
    # an existing issue is updated in place (the open one), and closed without conflicts
    existing[:] = [{"number": 3, "state": "closed", "html_url": "https://3"},
                   {"number": 7, "state": "open", "html_url": "https://7"}]
    assert maintenance.publish_report("o/r", "jazzy", "body", conflicts=False) == "https://7"
    assert calls[-1][:4] == ["api", "-X", "PATCH", "repos/o/r/issues/7"] and "state=closed" in calls[-1]

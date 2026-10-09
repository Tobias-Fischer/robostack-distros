"""CI cache, release ordering and live-label automation regressions (no publishing)."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_config
import release


REPO = Path(__file__).resolve().parents[2]


def command(root, *args):
    return subprocess.run(args, cwd=root, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.root = self.base / "repo"
        command(self.base, "git", "init", "--bare", str(self.remote))
        command(self.base, "git", "init", "-b", "main", str(self.root))
        command(self.root, "git", "config", "user.name", "CI test")
        command(self.root, "git", "config", "user.email", "ci@example.invalid")
        command(self.root, "git", "remote", "add", "origin", str(self.remote))
        self.put("distros/jazzy/ci.yaml", "full_rebuild: false\nevict_cache: []\n")
        self.put("distros/jazzy/vinca.yaml", "ros_distro: jazzy\n")
        self.put("distros/jazzy/conda_build_config.yaml", "python: ['3.12']\n")
        self.put("distros/jazzy/patch/fix.patch", "initial patch\n")
        self.put("distros/humble/vinca.yaml", "ros_distro: humble\n")
        self.put("pixi.lock", "initial lock\n")
        self.put("shared/vinca.yaml", "build_number: 1\n")
        self.put("README.md", "initial docs\n")
        self.initial = self.commit("initial inputs")
        command(self.root, "git", "push", "origin", "HEAD:main")

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def commit(self, message):
        command(self.root, "git", "add", ".")
        command(self.root, "git", "commit", "-m", message)
        return command(self.root, "git", "rev-parse", "HEAD")

    def main_change(self, name, text):
        command(self.root, "git", "checkout", "main")
        self.put(name, text)
        source = self.commit("new main inputs")
        command(self.root, "git", "push", "origin", "HEAD:main")
        return source

    def generate(self, source, marker="generated"):
        command(self.root, "git", "checkout", "--detach", source)
        self.put(".github/workflows/build.yaml", marker)
        return self.commit("generated build")

    def remote_head(self, branch):
        return command(self.base, "git", "--git-dir", str(self.remote), "rev-parse", branch)

    def publish_env(self, source, commit):
        return patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "GITHUB_SHA": commit,
            "GITHUB_REF": "refs/heads/buildbranch_jazzy_linux_64",
            "ROBOSTACK_SOURCE_SHA": source, "ROBOSTACK_PLATFORM": "linux-64",
        })


class CacheEpochTests(GitFixture):
    def epoch(self, platform="linux-64"):
        return ci_config.cache_epoch(self.root, "jazzy", platform)

    def test_full_rebuild_and_eviction_retries_keep_replacements(self):
        cache = self.base / "cache"
        original = self.epoch()
        ci_config.prepare_cache(cache, original)
        artifact = cache / "ros2-demo-1.0-0.conda"
        artifact.write_bytes(b"previous configuration")
        self.put("distros/jazzy/ci.yaml", "full_rebuild: true\nevict_cache: []\n")
        rebuild = self.epoch()
        self.assertNotEqual(original, rebuild)
        ci_config.prepare_cache(cache, rebuild)
        self.assertFalse(artifact.exists())
        artifact.write_bytes(b"successful rebuild")
        ci_config.prepare_cache(cache, self.epoch())
        self.assertEqual(artifact.read_bytes(), b"successful rebuild")
        self.put("distros/jazzy/ci.yaml", "full_rebuild: true\nevict_cache: [demo]\n")
        eviction = self.epoch()
        self.assertNotEqual(rebuild, eviction)
        ci_config.prepare_cache(cache, eviction)
        self.assertFalse(artifact.exists())
        artifact.write_bytes(b"successful eviction replacement")
        ci_config.prepare_cache(cache, self.epoch())
        self.assertEqual(artifact.read_bytes(), b"successful eviction replacement")

    def test_reset_controls_cannot_resurrect_a_pre_rebuild_epoch(self):
        original = self.epoch()
        self.put("distros/jazzy/ci.yaml", "full_rebuild: true\nevict_cache: []\n")
        self.commit("request rebuild")
        rebuild = self.epoch()
        self.put("distros/jazzy/ci.yaml", "full_rebuild: false\nevict_cache: []\n")
        self.commit("reset rebuild controls")
        self.assertNotEqual(original, self.epoch())
        self.assertNotEqual(rebuild, self.epoch())

    def test_recipe_patch_pins_lock_and_shared_inputs_invalidate(self):
        inputs = {
            "distros/jazzy/work/recipes/ros2-demo/recipe.yaml": "package: {name: ros2-demo}\n",
            "distros/jazzy/work/recipes/ros2-demo/build.sh": "echo building\n",
            "distros/jazzy/patch/fix.patch": "changed patch\n",
            "distros/jazzy/conda_build_config.yaml": "python: ['3.13']\n",
            "distros/jazzy/work/conda_build_config.yaml": "numpy: ['2']\n",
            "pixi.lock": "different dependency lock\n",
            "shared/vinca.yaml": "build_number: 2\n",
        }
        for path, content in inputs.items():
            with self.subTest(path=path):
                previous = self.epoch()
                self.put(path, content)
                self.assertNotEqual(previous, self.epoch())
        self.assertNotEqual(self.epoch(), self.epoch("win-64"))

    def test_unrelated_changes_do_not_discard_progress(self):
        epoch = self.epoch()
        self.put("distros/humble/vinca.yaml", "ros_distro: humble\nbuild_number: 22\n")
        self.put("README.md", "unrelated documentation\n")
        self.put("distros/jazzy/work/output/linux-64/demo.conda", "successful output\n")
        self.assertEqual(epoch, self.epoch())

    def test_unmarked_and_changed_epochs_cannot_reuse_artifacts(self):
        cache = self.base / "cache"
        cache.mkdir()
        artifact = cache / "old.conda"
        artifact.write_bytes(b"old")
        ci_config.prepare_cache(cache, self.epoch())
        self.assertFalse(artifact.exists())
        artifact.write_bytes(b"current")
        (cache / ci_config.MARKER).write_text("foreign epoch")
        ci_config.prepare_cache(cache, self.epoch())
        self.assertFalse(artifact.exists())


class ReleaseOrderingTests(GitFixture):
    def test_delayed_generator_cannot_replace_newer_branch(self):
        newer_source = self.main_change("distros/jazzy/vinca.yaml", "ros_distro: jazzy\nbuild_number: 2\n")
        newer_build = self.generate(newer_source)
        release.push_branch("jazzy", "linux-64", newer_source, self.root)
        self.generate(self.initial, "delayed build")
        with self.assertRaises(release.StaleRelease):
            release.push_branch("jazzy", "linux-64", self.initial, self.root)
        self.assertEqual(self.remote_head("buildbranch_jazzy_linux_64"), newer_build)

    def test_delayed_lock_holder_assumes_latest_pending_inputs(self):
        newest = self.main_change("distros/jazzy/vinca.yaml", "ros_distro: jazzy\nbuild_number: 9\n")
        command(self.root, "git", "checkout", "--detach", self.initial)
        # An old event replaced a newer pending run in the same distro group.
        # It must take over the newer run's work, not merely fail as stale.
        refreshed = release.refresh_source(self.root)
        self.assertEqual(refreshed, newest)
        build = self.generate(refreshed)
        release.push_branch("jazzy", "linux-64", refreshed, self.root)
        self.assertEqual(self.remote_head("buildbranch_jazzy_linux_64"), build)

    def test_newer_other_distro_does_not_drop_untouched_work(self):
        self.main_change("distros/humble/vinca.yaml", "ros_distro: humble\nbuild_number: 3\n")
        older_build = self.generate(self.initial)
        release.push_branch("jazzy", "linux-64", self.initial, self.root)
        with self.publish_env(self.initial, older_build):
            release.assert_publishable("jazzy", self.root)
        self.assertEqual(self.remote_head("buildbranch_jazzy_linux_64"), older_build)

    def test_new_relevant_main_input_blocks_existing_build_publication(self):
        build = self.generate(self.initial)
        release.push_branch("jazzy", "linux-64", self.initial, self.root)
        self.main_change("distros/jazzy/patch/fix.patch", "new patch\n")
        command(self.root, "git", "checkout", "--detach", build)
        with self.publish_env(self.initial, build), self.assertRaises(release.StaleRelease):
            release.assert_publishable("jazzy", self.root)

    def test_stale_manual_rerun_is_rejected_even_with_same_source(self):
        first = self.generate(self.initial, "first build")
        release.push_branch("jazzy", "linux-64", self.initial, self.root)
        second = self.generate(self.initial, "replacement build")
        release.push_branch("jazzy", "linux-64", self.initial, self.root)
        with self.publish_env(self.initial, second):
            release.assert_publishable("jazzy", self.root)
        command(self.root, "git", "checkout", "--detach", first)
        with self.publish_env(self.initial, first), self.assertRaises(release.StaleRelease):
            release.assert_publishable("jazzy", self.root)

    def test_push_lease_rejects_intervening_branch_update(self):
        original = self.generate(self.initial, "original")
        release.push_branch("jazzy", "linux-64", self.initial, self.root)
        replacement = self.generate(self.initial, "racing replacement")
        # Transfer the race's commit, but do not move the release branch yet.
        command(self.root, "git", "push", "origin", "HEAD:refs/heads/race-object")
        self.generate(self.initial, "candidate")
        real_git = release.git

        def race(root, *args):
            if args[0] == "push":
                command(self.base, "git", "--git-dir", str(self.remote), "update-ref",
                        "refs/heads/buildbranch_jazzy_linux_64", replacement, original)
            return real_git(root, *args)

        with patch.object(release, "git", side_effect=race), self.assertRaises(subprocess.CalledProcessError):
            release.push_branch("jazzy", "linux-64", self.initial, self.root)
        self.assertEqual(self.remote_head("buildbranch_jazzy_linux_64"), replacement)

    def test_actions_requires_metadata_but_local_upload_does_not(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=True):
            release.assert_publishable("jazzy", self.root)
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
            with self.assertRaisesRegex(release.StaleRelease, "metadata"):
                release.assert_publishable("jazzy", self.root)

    def test_generated_workflow_runs_a_real_freshness_fence(self):
        yaml = YAML()
        raw = {
            "name": "build_unix", "on": {"push": {"branches": ["buildbranch_jazzy_linux_64"]}},
            "env": {"KEEP_ME": "yes"},
            "jobs": {"first": {"permissions": {"id-token": "write", "attestations": "write"},
                                "steps": [{"uses": "actions/checkout@v6"},
                                          {"uses": "prefix-dev/setup-pixi@v0.11.0", "with": {"frozen": True}},
                                          {"run": "pixi run rs jazzy build-ci"}]},
                     "second": {"needs": ["first"], "steps": [{"uses": "actions/checkout@v6"}]}}}
        stream = io.StringIO()
        yaml.dump(raw, stream)
        with patch.object(release, "ROOT", self.root):
            configured = yaml.load(release.configure_workflow(stream.getvalue(), "jazzy", "linux-64"))
        self.assertEqual(configured["on"], raw["on"])
        self.assertEqual(configured["jobs"]["second"]["needs"], ["first"])
        self.assertEqual(configured["env"]["KEEP_ME"], "yes")
        self.assertEqual(configured["concurrency"]["queue"], "max")
        self.assertFalse(configured["concurrency"]["cancel-in-progress"])
        self.assertEqual(configured["env"]["PIXI_LOCKED"], "true")
        first = configured["jobs"]["first"]
        self.assertEqual(first["permissions"], {"contents": "read", "id-token": "write", "attestations": "write"})
        self.assertEqual(first["steps"][2]["with"], {"locked": True})
        self.put("tools/release.py", (REPO / "tools/release.py").read_text())
        commit = self.commit("generated workflow and helper")
        release.push_branch("jazzy", "linux-64", self.initial, self.root)
        environment = {**os.environ, **configured["env"], "GITHUB_ACTIONS": "true", "GITHUB_SHA": commit,
                       "GITHUB_REF": "refs/heads/buildbranch_jazzy_linux_64"}
        result = subprocess.run(["bash", "-e", "-c", first["steps"][1]["run"]], cwd=self.root,
                                env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.main_change("distros/jazzy/patch/fix.patch", "superseded\n")
        command(self.root, "git", "checkout", "--detach", commit)
        result = subprocess.run(["bash", "-e", "-c", first["steps"][1]["run"]], cwd=self.root,
                                env=environment, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newer build inputs", result.stderr)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        # This executable is the GitHub API boundary. The workflow shell itself is
        # executed unchanged, including its real live-label and credential logic.
        gh = self.root / "gh"
        gh.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
state_path = pathlib.Path(os.environ["STATE"])
state = json.loads(state_path.read_text())
if sys.argv[1:3] == ["pr", "view"]:
    print(str("automerge" in state["labels"]).lower())
elif sys.argv[1:3] == ["pr", "merge"]:
    state["enabled"] = "--auto" in sys.argv
    state["token"] = os.environ["GH_TOKEN"]
    state_path.write_text(json.dumps(state))
else:
    sys.exit(3)
''')
        gh.chmod(0o755)
        workflow = YAML().load((REPO / ".github/workflows/automerge.yaml").read_text())
        self.script = next(step["run"] for step in workflow["jobs"]["automerge"]["steps"] if "run" in step)

    def run_workflow(self, action, token="bot-token"):
        env = {**os.environ, "PATH": f"{self.root}{os.pathsep}{os.environ['PATH']}",
               "STATE": str(self.state), "GH_TOKEN": token or "github-token", "BOT_TOKEN": token,
               "PR": "https://github.com/example/repo/pull/1", "ACTION": action}
        return subprocess.run(["bash", "-e", "-c", self.script], env=env, capture_output=True, text=True)

    def test_late_labeled_event_does_not_reenable_removed_label(self):
        self.state.write_text(json.dumps({"labels": [], "enabled": True}))
        for action in ("unlabeled", "labeled"):
            result = self.run_workflow(action)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(self.state.read_text())["enabled"])

    def test_late_unlabeled_event_honors_current_label(self):
        self.state.write_text(json.dumps({"labels": ["automerge"], "enabled": False}))
        result = self.run_workflow("unlabeled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(self.state.read_text())["enabled"])
        self.assertEqual(json.loads(self.state.read_text())["token"], "bot-token")

    def test_missing_bot_token_cannot_enable_but_can_disable(self):
        self.state.write_text(json.dumps({"labels": ["automerge"], "enabled": False}))
        result = self.run_workflow("labeled", token="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GHA_PAT", result.stdout)
        self.assertFalse(json.loads(self.state.read_text())["enabled"])
        self.state.write_text(json.dumps({"labels": [], "enabled": True}))
        result = self.run_workflow("unlabeled", token="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(self.state.read_text())["enabled"])

    def test_bot_pr_credential_guard_fails_clearly_without_fallback(self):
        workflow = YAML().load((REPO / ".github/workflows/bot.yaml").read_text())
        script = next(step["run"] for step in workflow["jobs"]["run"]["steps"]
                      if step.get("name") == "Require credentials that trigger unattended PR checks")
        for token, expected in (("", 1), ("app-token", 0)):
            result = subprocess.run(["bash", "-e", "-c", script],
                                    env={**os.environ, "BOT_TOKEN": token}, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            if not token:
                self.assertIn("require approval", result.stdout)
                self.assertIn("GHA_PAT", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""Behavioral regressions for local recipe inputs and upload authentication."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import robostack as rs


class RecipeInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="robostack inputs ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("distros/test/patch", "shared/pinning", "shared/patch", ".scripts", "tools"):
            (self.root / name).mkdir(parents=True)
        files = {
            "pixi.toml": "[workspace]\nname = 'test'\n",
            "pixi.lock": "version: 6\n",
            "tools/robostack.py": "# generator\n",
            "distros/test/vinca.yaml": "ros_distro: test\n",
            "distros/test/distro.yaml": "upload_target: anaconda\nchannel_name: test\n",
            "distros/test/conda_build_config.yaml": "python:\n  - 3.14\n",
            "shared/pinning/conda_forge.yaml": "conda_forge_pinning_version: test\nmigrations: []\n",
            "shared/pinning/overrides.yaml": "pinning_overrides: {}\n",
            "shared/patch/ros2-example.patch": "old patch\n",
        }
        for name, text in files.items():
            (self.root / name).write_text(text)
        for field, value in {
            "ROOT": self.root, "DISTROS": self.root / "distros", "SHARED": self.root / "shared", "TOOLS": self.root / "tools",
        }.items():
            p = patch.object(rs, field, value)
            p.start()
            self.addCleanup(p.stop)
        self.work = rs.prepare("test")

    def generated(self):
        recipe = self.work / "recipes/ros2-example/recipe.yaml"
        recipe.parent.mkdir(parents=True)
        recipe.write_text("package:\n  name: ros2-example\n  version: 1\n")
        (self.work / "recipes-state.json").write_text(json.dumps({
            "fingerprint": rs.recipe_fingerprint("test"), "platform": "linux-64",
        }))
        artifact = self.work / "output/linux-64/ros2-built-1-h0_0.conda"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"existing build output")
        return artifact

    def test_fresh_checkout_fails_before_starting_builder(self):
        with patch.object(rs, "run") as run:
            with self.assertRaisesRegex(SystemExit, "generate-recipes --platform linux-64"):
                rs.task("test", "build-one", ["ros2-example", "--target-platform", "linux-64"])
        run.assert_not_called()

    def test_changed_inputs_refresh_config_but_reject_stale_recipes(self):
        artifact = self.generated()
        cbc = self.root / "distros/test/conda_build_config.yaml"
        cbc.write_text("python:\n  - 3.15\n")
        with patch.object(rs, "run") as run:
            with self.assertRaisesRegex(SystemExit, "out of date"):
                rs.task("test", "build", ["--target-platform", "linux-64"])
        run.assert_not_called()
        self.assertEqual((self.work / "conda_build_config.yaml").read_text(), cbc.read_text())
        self.assertEqual(artifact.read_bytes(), b"existing build output")

    def test_shared_patch_change_invalidates_recipes_without_deleting_outputs(self):
        artifact = self.generated()
        (self.root / "shared/patch/ros2-example.patch").write_text("changed patch\n")
        with self.assertRaisesRegex(SystemExit, "out of date"):
            rs.require_recipes("test", "linux-64", "ros2-example")
        self.assertEqual(artifact.read_bytes(), b"existing build output")

    def test_wrong_platform_and_missing_package_are_actionable(self):
        self.generated()
        with self.assertRaisesRegex(SystemExit, "generate-recipes --platform win-64"):
            rs.require_recipes("test", "win-64")
        with self.assertRaisesRegex(SystemExit, "No generated recipe for ros2-absent"):
            rs.require_recipes("test", "linux-64", "ros2-absent")

    def test_partial_generation_state_is_rejected(self):
        self.generated()
        (self.work / "recipes-state.json").write_text("{incomplete")
        with self.assertRaisesRegex(SystemExit, "out of date"):
            rs.require_recipes("test", "linux-64")


class UploadTests(unittest.TestCase):
    def test_upload_credentials_never_appear_in_command_or_retry_logs(self):
        sentinel = "audit-token-must-not-be-logged"
        attempts = []

        def uploader(cmd, cwd, env):
            attempts.append((cmd, env))
            # The real subprocess boundary receives authentication via its environment.
            self.assertEqual(env["ANACONDA_API_KEY"], sentinel)
            self.assertNotIn("ANACONDA_API_TOKEN", env)
            self.assertNotIn(sentinel, " ".join(cmd))
            return type("Result", (), {"returncode": 1 if len(attempts) == 1 else 0})()

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"ANACONDA_API_TOKEN": sentinel}, clear=True), \
                patch.object(rs, "settings", return_value={"upload_target": "anaconda", "channel_name": "test"}), \
                patch("release.assert_publishable"), patch.object(rs.subprocess, "run", side_effect=uploader), \
                patch.object(rs.time, "sleep"), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = rs.upload("test", ["example.conda"], rs.ROOT)
            self.assertEqual(os.environ["ANACONDA_API_TOKEN"], sentinel)
            self.assertNotIn("ANACONDA_API_KEY", os.environ)
        self.assertEqual(rc, 0)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn(sentinel, stdout.getvalue() + stderr.getvalue())
        self.assertIn("retrying", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

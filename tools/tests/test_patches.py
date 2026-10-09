"""Patch checks use effective Vinca layers, not generated host-only references."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


TOOLS = Path(__file__).resolve().parents[1]
VALID_PATCH = """--- a/message.txt
+++ b/message.txt
@@ -1 +1 @@
-before
+after
"""


class PatchChecksTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shared = self.root / "shared"
        self.distro = self.root / "distro"
        self.work = self.root / "work"
        for directory in (self.shared / "patch", self.distro / "patch", self.work):
            directory.mkdir(parents=True)
        self.write_yaml(self.shared / "vinca.yaml", {
            "ros_distro": "jazzy", "patch_dir": "patch", "conda_index": [],
            "package_name_mode": "both",
        })
        self.write_yaml(self.distro / "vinca.yaml", {
            "extends": "../shared/vinca.yaml", "patch_dir": "patch",
        })
        self.write_yaml(self.work / "vinca.yaml", {"extends": "../distro/vinca.yaml"})
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "message.txt").write_text("before\n", encoding="utf-8")

    def write_yaml(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(content), encoding="utf-8")

    def patch_file(self, directory, name, content=VALID_PATCH):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def recipe(self, name="ros2-demo", patches=None, source_list=False):
        source = {"path": str(self.source)}
        if patches is not None:
            source["patches"] = patches
        path = self.work / "recipes" / name / "recipe.yaml"
        self.write_yaml(path, {
            "package": {"name": name, "version": "1.0.0"},
            "source": [source] if source_list else source,
            "build": {"number": 9, "script": "must-not-run"},
            "requirements": {"host": ["must-not-resolve"]},
        })
        return path

    def run_check(self, *args, orphan=False):
        script = "check_orphaned_platform_patches.py" if orphan else "check_patches_clean_apply.py"
        return subprocess.run(
            [sys.executable, str(TOOLS / script), *args], cwd=self.work,
            text=True, capture_output=True, timeout=120,
        )

    def prepared(self):
        return sorted((self.work / "recipes_only_patch").rglob("recipe.yaml"))

    def patch_sets(self):
        sets = []
        for path in self.prepared():
            recipe = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertNotIn("requirements", recipe)
            self.assertEqual(recipe["build"]["script"], "echo patch-check")
            sets.append(tuple(
                (Path(ref).name, (path.parent / ref).read_text(encoding="utf-8"))
                for ref in recipe["source"][0]["patches"]
            ))
        return sets

    def test_shared_patch_is_prepared_from_work_config(self):
        name = "ros2-swri-serial-util"
        self.patch_file(self.shared / "patch", f"{name}.patch")
        self.recipe(name, patches=[f"patch/{name}.patch"])
        result = self.run_check("--dry", "--recipe", name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.patch_sets(), [((f"{name}.patch", VALID_PATCH),)])

    def test_override_replaces_entire_shared_package_bucket(self):
        self.patch_file(self.shared / "patch", "ros2-demo.patch", "obsolete generic")
        self.patch_file(self.shared / "patch", "ros2-demo.win.patch", "obsolete windows")
        self.patch_file(self.distro / "patch", "ros-jazzy-demo.unix.patch")
        self.recipe(patches=["patch/ros2-demo.patch"], source_list=True)
        result = self.run_check("--dry", "--vinca", str(self.work / "vinca.yaml"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.patch_sets(), [(("ros-jazzy-demo.unix.patch", VALID_PATCH),)])

    def test_all_platform_variants_without_host_recipe_patches(self):
        for suffix in ("unix", "linux", "win", "emscripten"):
            self.patch_file(self.shared / "patch", f"ros-jazzy-demo.{suffix}.patch")
        self.recipe()
        result = self.run_check("--dry")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = {tuple(name for name, _ in variant) for variant in self.patch_sets()}
        self.assertEqual(actual, {
            ("ros-jazzy-demo.linux.patch", "ros-jazzy-demo.unix.patch"),
            ("ros-jazzy-demo.unix.patch",), ("ros-jazzy-demo.win.patch",),
            ("ros-jazzy-demo.emscripten.patch",),
        })
        self.assertEqual(len(self.prepared()), 4)

    def test_generic_precedes_platform_patch(self):
        self.patch_file(self.shared / "patch", "ros2-demo.patch", "generic")
        self.patch_file(self.shared / "patch", "ros2-demo.win.patch", "windows")
        self.recipe()
        result = self.run_check("--dry")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(self.patch_sets()), {
            (("ros2-demo.patch", "generic"),),
            (("ros2-demo.patch", "generic"), ("ros2-demo.win.patch", "windows")),
        })

    def test_architecture_selector_resolves_distinct_patch_directories(self):
        self.write_yaml(self.distro / "vinca.yaml", {
            "extends": "../shared/vinca.yaml",
            "patch_dir": {"if": "target_platform == 'osx-arm64'", "then": "arm", "else": "patch"},
        })
        self.patch_file(self.shared / "patch", "ros2-demo.patch", "shared")
        self.patch_file(self.distro / "arm", "ros2-demo.patch", "arm override")
        self.recipe()
        result = self.run_check("--dry")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(self.patch_sets()), {
            (("ros2-demo.patch", "shared"),), (("ros2-demo.patch", "arm override"),),
        })

    def test_orphan_checker_finds_inherited_literal_prefix_collision(self):
        self.patch_file(self.shared / "patch", "ros2-demo.patch")
        self.patch_file(self.shared / "patch", "ros-jazzy-demo.osx.patch")
        result = self.run_check("--vinca", str(self.work / "vinca.yaml"), orphan=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ros-jazzy-demo.osx.patch", result.stderr)
        self.assertIn("ros2-demo.patch", result.stderr)

    def test_orphan_checker_ignores_aliases_and_shadowed_shared_patches(self):
        self.patch_file(self.shared / "patch", "ros2-demo.patch")
        self.patch_file(self.shared / "patch", "ros-jazzy-demo.osx.patch")
        self.patch_file(self.distro / "patch", "ros-demo.win.patch")
        result = self.run_check(orphan=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 effective patch files scanned", result.stdout)

    def test_orphan_checker_finds_disjoint_platform_prefix_collision(self):
        self.patch_file(self.shared / "patch", "ros2-demo.linux.patch")
        self.patch_file(self.shared / "patch", "ros-jazzy-demo.osx.patch")
        result = self.run_check(orphan=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ros-jazzy-demo.osx.patch", result.stderr)
        self.assertIn("ros2-demo.linux.patch", result.stderr)

    def test_orphan_checker_does_not_mix_target_specific_prefixes(self):
        self.write_yaml(self.distro / "vinca.yaml", {
            "extends": "../shared/vinca.yaml",
            "patch_dir": {"if": "win", "then": "windows", "else": "patch"},
        })
        self.patch_file(self.shared / "patch", "ros2-demo.patch")
        self.patch_file(self.distro / "windows", "ros-jazzy-demo.win.patch")
        result = self.run_check(orphan=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("2 effective patch files scanned", result.stdout)

    def test_missing_inherited_config_is_an_error(self):
        (self.shared / "vinca.yaml").unlink()
        self.recipe()
        for orphan in (False, True):
            with self.subTest(orphan=orphan):
                args = () if orphan else ("--dry",)
                result = self.run_check(*args, orphan=orphan)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("vinca.yaml", result.stderr)

    @unittest.skipUnless(shutil.which("rattler-build"), "requires rattler-build from the pixi environment")
    def test_nonapplying_shared_patch_fails_actual_build(self):
        patch = self.patch_file(self.shared / "patch", "ros2-demo.patch")
        self.recipe()
        valid = self.run_check("--recipe", "ros2-demo")
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        self.assertIn("Passed: 1", valid.stdout)
        patch.write_text(VALID_PATCH.replace("-before", "-missing upstream line"), encoding="utf-8")
        invalid = self.run_check("--recipe", "ros2-demo")
        self.assertEqual(invalid.returncode, 2, invalid.stdout + invalid.stderr)
        self.assertIn("Failed: 1", invalid.stdout)


if __name__ == "__main__":
    unittest.main()

"""Maintenance regressions using temporary repositories and external-tool boundaries."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maintenance as maintenance
import robostack as rs


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("distros", "shared/pinning", ".scripts", ".github/workflows", "tools"):
            (self.root / name).mkdir(parents=True)
        (self.root / "tools/ci_default.yaml").write_text((rs.TOOLS / "ci_default.yaml").read_text())
        for attr, relative in (("ROOT", "."), ("DISTROS", "distros"), ("SHARED", "shared"), ("TOOLS", "tools")):
            self.stack.enter_context(patch.object(rs, attr, self.root / relative))
        self.dump(rs.SHARED / "vinca.yaml", {
            "package_name_mode": "new", "packages_select_by_deps": ["alpha", "zeta"],
        })
        self.dump(rs.SHARED / "pinning/conda_forge.yaml", {
            "conda_forge_pinning_version": "2026.01", "migrations": [],
        })
        self.dump(rs.SHARED / "pinning/overrides.yaml", {"pinning_overrides": {}})
        self.make_distro("jazzy")

    def dump(self, path, value):
        path.write_text(yaml.safe_dump(value, sort_keys=False))

    def load(self, path):
        return yaml.safe_load(path.read_text())

    def make_distro(self, name, own=False):
        directory = rs.DISTROS / name
        directory.mkdir()
        (directory / "patch").mkdir()
        settings = {"channel_name": f"robostack-{name}"}
        if own:
            settings.update(conda_forge_pinning_version="2026.01", conda_forge_migrations=[])
        self.dump(directory / "distro.yaml", settings)
        self.dump(directory / "vinca.yaml", {
            "extends": "../../shared/vinca.yaml", "ros_distro": name,
            "build_number": 5, "patch_dir": "patch",
            "rosdistro_snapshot": "rosdistro_snapshot.yaml",
            "rosdistro_additional_recipes": "rosdistro_additional_recipes.yaml",
            "mutex_package": {"name": "ros2-distro-mutex", "version": "0.1.0",
                              "run_constraints": ["libfoo 1.0.*"]},
        })
        self.dump(directory / "pkg_additional_info.yaml", {
            "alpha": {"build_number": 6, "extra": "keep"}, "zeta": {"build_number": 6},
        })
        self.dump(directory / "rosdistro_snapshot.yaml", {
            name: {"version": "1.0.0"} for name in ("alpha", "zeta", "middle", "rosidlcpp", "linux_only", "skipped")
        })
        self.dump(directory / "rosdistro_additional_recipes.yaml", {})
        self.dump(directory / "conda_build_config.yaml", {"libfoo": ["1.0"]})
        self.dump(directory / "ci.yaml", {"full_rebuild": False, "evict_cache": ["alpha"]})
        return directory

    def files(self, directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}


class PinningRenderTests(RepositoryTest):
    def test_owned_pins_with_no_migrations_render_with_real_vinca(self):
        from vinca.pinning import render_pinning

        (rs.SHARED / "pinning/overrides.yaml").write_text("pinning_overrides:\n")
        self.dump(rs.DISTROS / "jazzy/distro.yaml", {
            "conda_forge_pinning_version": "audit",
            "conda_forge_migrations": [],
            "pinning_overrides": {"python": ["3.12.*"]},
        })
        work = rs.prepare("jazzy")
        output = work / "rendered.yaml"
        render_pinning(
            work / "vinca_pinning.yaml", output,
            package=(b"python:\n  - 3.14.*\n", {}),
        )
        self.assertEqual(self.load(output)["python"], ["3.12.*"])


class SelectionTests(RepositoryTest):
    def test_excluded_conditional_selection_is_not_broadened(self):
        shared = rs.SHARED / "vinca.yaml"
        data = self.load(shared)
        data["packages_select_by_deps"].append({"if": "linux", "then": ["rosidlcpp"]})
        self.dump(shared, data)
        path = rs.DISTROS / "jazzy/vinca.yaml"
        data = self.load(path)
        data["packages_exclude"] = ["rosidlcpp"]
        self.dump(path, data)
        before = self.files(self.root)
        result = maintenance.add_package(["rosidlcpp"], ["jazzy"])
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertIn("excluded", result.summary)
        self.assertEqual(self.files(self.root), before)

    def test_shared_and_distro_conditions_and_skip_are_preserved(self):
        for location, key, entry, message in (
            ("shared", "packages_select_by_deps", {"if": "linux", "then": ["linux_only"]}, "conditions"),
            ("distro", "packages_select_by_deps", {"if": "osx", "then": ["linux_only"]}, "conditions"),
            ("distro", "packages_skip", {"if": "win", "then": ["linux_only"]}, "skipped"),
            ("shared", "packages_exclude", ["linux_only"], "excluded"),
        ):
            with self.subTest(location=location, key=key):
                path = rs.SHARED / "vinca.yaml" if location == "shared" else rs.DISTROS / "jazzy/vinca.yaml"
                original = path.read_bytes()
                data = self.load(path)
                data.setdefault(key, []).extend(entry if isinstance(entry, list) else [entry])
                self.dump(path, data)
                before = self.files(self.root)
                result = maintenance.add_package(["linux_only"], ["jazzy"])
                self.assertFalse(result.changed)
                self.assertIn(message, result.summary)
                self.assertEqual(self.files(self.root), before)
                path.write_bytes(original)

    def test_other_distro_selection_is_not_broadened_by_shared_addition(self):
        other = self.make_distro("humble")
        path = other / "vinca.yaml"
        data = self.load(path)
        data["packages_select_by_deps"] = [{"if": "linux", "then": ["linux_only"]}]
        self.dump(path, data)
        before = self.files(self.root)
        result = maintenance.add_package(["linux_only"], ["jazzy"])
        self.assertFalse(result.changed)
        self.assertIn("scope", result.summary)
        self.assertEqual(self.files(self.root), before)

    def test_absent_package_is_added_once_in_sorted_order(self):
        def run(command, cwd):
            recipes = cwd / "recipes"
            for name in rs.read_vinca("jazzy", "linux-64")["packages_select_by_deps"]:
                (recipes / f"ros2-{name.replace('_', '-')}").mkdir(exist_ok=True)
            return 0

        with patch.object(rs, "run", side_effect=run), patch.object(
            maintenance.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ):
            result = maintenance.add_package(["middle", "ros2-middle"], ["jazzy"])
            self.assertTrue(result.ok)
            self.assertTrue(result.changed)
            self.assertIn("1 new package", result.summary)
            self.assertEqual(self.load(rs.SHARED / "vinca.yaml")["packages_select_by_deps"], ["alpha", "middle", "zeta"])
            before = self.files(self.root)
            again = maintenance.add_package(["middle"], ["jazzy"])
            self.assertFalse(again.changed)
            self.assertEqual(self.files(self.root), before)


class PinningTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        from vinca import pinning
        self.version = "2026.02"
        self.render_rc = 0
        self.fail_check = False
        self.checks = []
        self.plan = ({"alpha": "libfoo changed"}, 2)
        self.stack.enter_context(patch.object(pinning, "dependencies_from_vinca", return_value={"libfoo"}))
        self.stack.enter_context(patch.object(pinning, "update_pinning", side_effect=self.propose))
        self.stack.enter_context(patch.object(rs, "run", side_effect=self.run_tool))
        self.stack.enter_context(patch.object(maintenance, "plan_rebuild", side_effect=lambda *args: self.plan))
        self.stack.enter_context(patch.object(maintenance, "released_builds", return_value={
            "ros2-alpha": 7, "ros2-zeta": 7, "ros2-distro-mutex": 2,
        }))
        rs.prepare("jazzy")

    def propose(self, path, **kwargs):
        data = self.load(path)
        data.update(conda_forge_pinning_version=self.version, migrations=[])
        self.dump(path, data)
        return self.version, [], []

    def run_tool(self, command, cwd):
        if command[0] == "vinca-pinning-render":
            self.dump(cwd / "conda_build_config.yaml", {"libfoo": ["2.0"]})
            return self.render_rc
        if "--json" in command:
            self.checks.append(self.load(cwd / "conda_build_config.yaml"))
            if self.fail_check and len(self.checks) == 2:
                # Fail after the actual mutex and package build-number mutations.
                self.dump(rs.DISTROS / "jazzy/ci.yaml", {"full_rebuild": True})
                raise RuntimeError("solver unavailable")
            return 0
        raise AssertionError(f"Unexpected external command: {command}")

    def test_failed_renderer_restores_sources_and_generated_state(self):
        self.render_rc = 1
        before = self.files(self.root)
        result = maintenance.update_pinning("jazzy")
        self.assertFalse(result.ok)
        self.assertFalse(result.changed)
        self.assertIn("render-pinning failed", result.summary)
        self.assertNotIn("nothing to rebuild", result.summary)
        self.assertEqual(self.files(self.root), before)

    def test_failure_after_partial_bump_restores_all_state(self):
        self.fail_check = True
        before = self.files(self.root)
        result = maintenance.update_pinning("jazzy")
        self.assertFalse(result.ok)
        self.assertIn("solver unavailable", result.summary)
        self.assertEqual(self.files(self.root), before)

    def test_failure_after_full_bump_restores_all_state(self):
        self.plan = ({"alpha": "libfoo changed", "zeta": "depends on alpha"}, 2)
        self.fail_check = True
        before = self.files(self.root)
        result = maintenance.update_pinning("jazzy")
        self.assertFalse(result.ok)
        self.assertEqual(self.files(self.root), before)

    def test_failed_update_removes_new_package_and_ci_overrides(self):
        directory = rs.DISTROS / "jazzy"
        (directory / "pkg_additional_info.yaml").unlink()
        (directory / "ci.yaml").unlink()
        self.fail_check = True
        before = self.files(self.root)
        result = maintenance.update_pinning("jazzy")
        self.assertFalse(result.ok)
        self.assertEqual(self.files(self.root), before)

    def test_planning_error_restores_rendered_and_mutex_changes(self):
        before = self.files(self.root)
        with patch.object(maintenance, "plan_rebuild", side_effect=RuntimeError("planner failed")):
            result = maintenance.update_pinning("jazzy")
        self.assertFalse(result.ok)
        self.assertIn("planner failed", result.summary)
        self.assertEqual(self.files(self.root), before)

    def test_failed_render_does_not_leave_new_generated_files(self):
        for relative in ("conda_build_config.yaml", "work/conda_build_config.yaml"):
            (rs.DISTROS / "jazzy" / relative).unlink()
        self.render_rc = 1
        # The old-pin dependency check cannot load an absent CBC in this fixture.
        with patch.object(maintenance, "dependency_conflicts", return_value=None):
            before = self.files(self.root)
            result = maintenance.update_pinning("jazzy")
        self.assertFalse(result.ok)
        self.assertEqual(self.files(self.root), before)

    def test_noop_shared_follower_keeps_inputs_and_work_bytes(self):
        self.version = "2026.01"
        before = self.files(self.root)
        result = maintenance.update_pinning("jazzy")
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(self.files(self.root), before)
        self.assertNotIn("conda_forge_pinning_version", rs.settings("jazzy"))

    def test_changed_follower_owns_pins_without_touching_other_consumers(self):
        follower = self.make_distro("humble")
        owner = self.make_distro("rolling", own=True)
        rs.prepare("humble")
        rs.prepare("rolling")
        untouched = {directory: self.files(directory) for directory in (rs.SHARED, follower, owner)}
        result = maintenance.update_pinning("jazzy")
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertIn("now owns its pinning", result.summary)
        self.assertEqual(rs.settings("jazzy")["conda_forge_pinning_version"], "2026.02")
        self.assertEqual(rs.settings("jazzy")["conda_forge_migrations"], [])
        for directory, before in untouched.items():
            self.assertEqual(self.files(directory), before)
        self.assertEqual(self.checks, [{"libfoo": ["1.0"]}, {"libfoo": ["2.0"]}])
        directory = rs.DISTROS / "jazzy"
        config = self.load(directory / "vinca.yaml")
        self.assertEqual(config["build_number"], 5)
        self.assertEqual(config["mutex_package"]["version"], "0.1.0")
        self.assertEqual(config["mutex_package"]["build_number"], 3)
        self.assertEqual(config["mutex_package"]["run_constraints"], ["libfoo 2.0.*"])
        info = self.load(directory / "pkg_additional_info.yaml")
        self.assertEqual(info["alpha"]["build_number"], 8)
        self.assertEqual(info["alpha"]["extra"], "keep")
        self.assertEqual(info["zeta"]["build_number"], 6)
        self.assertEqual((directory / "conda_build_config.yaml").read_bytes(), (directory / "work/conda_build_config.yaml").read_bytes())

    def test_full_rebuild_keeps_existing_semantics(self):
        self.plan = ({"alpha": "libfoo changed", "zeta": "depends on alpha"}, 2)
        result = maintenance.update_pinning("jazzy")
        self.assertTrue(result.ok)
        self.assertIn("Full rebuild", result.summary)
        directory = rs.DISTROS / "jazzy"
        config = self.load(directory / "vinca.yaml")
        self.assertEqual(config["build_number"], 8)
        self.assertEqual(config["mutex_package"]["version"], "0.2.0")
        self.assertEqual(self.load(directory / "pkg_additional_info.yaml"), {"alpha": {"extra": "keep"}})

    def test_unchanged_render_updates_source_without_rebuilding(self):
        def render(command, cwd):
            if command[0] == "vinca-pinning-render":
                return 0
            return self.run_tool(command, cwd)
        before = (rs.DISTROS / "jazzy/vinca.yaml").read_bytes()
        with patch.object(rs, "run", side_effect=render):
            result = maintenance.update_pinning("jazzy")
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertIn("nothing to rebuild", result.summary)
        self.assertEqual((rs.DISTROS / "jazzy/vinca.yaml").read_bytes(), before)


class CheckTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(rs, "run", return_value=0))
        self.stack.enter_context(patch.object(maintenance.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")))

    def test_zero_build_number_and_empty_migrations_are_valid(self):
        config_path = rs.DISTROS / "jazzy/vinca.yaml"
        config = self.load(config_path)
        config["build_number"] = 0
        self.dump(config_path, config)
        settings_path = rs.DISTROS / "jazzy/distro.yaml"
        settings = self.load(settings_path)
        settings.update(conda_forge_pinning_version="2026.01", conda_forge_migrations=[])
        self.dump(settings_path, settings)
        result = maintenance.check()
        self.assertTrue(result.ok, result.summary)

    def test_invalid_and_missing_build_numbers_fail_usefully(self):
        path = rs.DISTROS / "jazzy/vinca.yaml"
        for value in (None, -1, True, False, "0", 1.5, [], {}):
            with self.subTest(value=value):
                config = self.load(path)
                if value is None:
                    config.pop("build_number", None)
                else:
                    config["build_number"] = value
                self.dump(path, config)
                result = maintenance.check()
                self.assertFalse(result.ok)
                self.assertIn("build_number", result.summary)
                self.assertIn("nonnegative integer", result.summary)

    def test_invalid_pinning_fields_fail_usefully(self):
        cases = (
            {"conda_forge_pinning_version": "2026.01"},
            {"conda_forge_migrations": []},
            {"conda_forge_pinning_version": None, "conda_forge_migrations": []},
            {"conda_forge_pinning_version": True, "conda_forge_migrations": []},
            {"conda_forge_pinning_version": "", "conda_forge_migrations": []},
            {"conda_forge_pinning_version": "2026.01", "conda_forge_migrations": None},
            {"conda_forge_pinning_version": "2026.01", "conda_forge_migrations": "foo"},
            {"conda_forge_pinning_version": "2026.01", "conda_forge_migrations": [False]},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                self.dump(rs.DISTROS / "jazzy/distro.yaml", fields)
                result = maintenance.check()
                self.assertFalse(result.ok)
                self.assertIn("conda_forge_", result.summary)


class NewDistroTests(RepositoryTest):
    def generate(self, distro, task, args):
        directory = rs.DISTROS / distro
        if task == "create-snapshot":
            self.dump(directory / "rosdistro_snapshot.yaml", {"alpha": {"version": "1.0.0"}})
        elif task == "render-pinning":
            self.dump(directory / "conda_build_config.yaml", {"libfoo": ["1.0"]})
        else:
            raise AssertionError(task)
        return 0

    def test_unknown_source_and_invalid_names_create_nothing(self):
        before = self.files(self.root)
        for name, source in (("future", "typo"), ("../outside", "jazzy"), ("future", "../jazzy"), ("", "jazzy"), ("UPPER", "jazzy")):
            with self.subTest(name=name, source=source):
                result = maintenance.new_distro(name, source)
                self.assertFalse(result.ok)
                self.assertFalse(result.changed)
                self.assertEqual(self.files(self.root), before)
                self.assertFalse((rs.DISTROS / "future").exists())

    def test_missing_required_input_is_rejected_before_destination_creation(self):
        (rs.DISTROS / "jazzy/pkg_additional_info.yaml").unlink()
        result = maintenance.new_distro("future", "jazzy")
        self.assertFalse(result.ok)
        self.assertIn("pkg_additional_info.yaml", result.summary)
        self.assertFalse((rs.DISTROS / "future").exists())

    def test_generator_failures_and_exceptions_allow_corrected_retry(self):
        for step, raises in (("create-snapshot", False), ("render-pinning", False), ("render-pinning", True)):
            with self.subTest(step=step, raises=raises):
                name = "future"
                before = self.files(rs.DISTROS / "jazzy")
                seen = []

                def fail(distro, task, args):
                    seen.append(task)
                    self.generate(distro, task, args)
                    if task == step:
                        if raises:
                            raise RuntimeError("generator crashed")
                        return 1
                    return 0

                with patch.object(rs, "task", side_effect=fail):
                    result = maintenance.new_distro(name, "jazzy")
                self.assertFalse(result.ok)
                self.assertFalse(result.changed)
                self.assertFalse((rs.DISTROS / name).exists())
                self.assertEqual(self.files(rs.DISTROS / "jazzy"), before)
                if step == "create-snapshot":
                    self.assertEqual(seen, ["create-snapshot"])
                with patch.object(rs, "task", side_effect=self.generate):
                    retry = maintenance.new_distro(name, "jazzy")
                self.assertTrue(retry.ok, retry.summary)
                self.assertTrue(retry.changed)
                directory = rs.DISTROS / name
                self.assertEqual(self.load(directory / "vinca.yaml")["build_number"], 0)
                self.assertEqual(self.load(directory / "vinca.yaml")["ros_distro"], name)
                self.assertNotIn("package_name_mode", self.load(directory / "vinca.yaml"))
                self.assertEqual(self.load(directory / "pkg_additional_info.yaml"), {"alpha": {"extra": "keep"}})
                self.assertNotIn("conda_forge_pinning_version", rs.settings(name))
                with patch.object(rs, "run", return_value=0), patch.object(
                    maintenance.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
                ):
                    check = maintenance.check()
                self.assertTrue(check.ok, check.summary)
                # Each subcase uses a fresh name without touching an existing distro.
                import shutil
                shutil.rmtree(directory)

    def test_existing_destination_and_broken_symlink_are_never_removed(self):
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                destination = rs.DISTROS / kind
                if kind == "directory":
                    destination.mkdir()
                    (destination / "sentinel").write_text("preserve me")
                elif kind == "file":
                    destination.write_text("preserve me")
                else:
                    destination.symlink_to(self.root / "missing")
                result = maintenance.new_distro(kind, "jazzy")
                self.assertFalse(result.ok)
                self.assertIn("exists", result.summary)
                if kind == "directory":
                    self.assertEqual((destination / "sentinel").read_text(), "preserve me")
                elif kind == "file":
                    self.assertEqual(destination.read_text(), "preserve me")
                else:
                    self.assertTrue(destination.is_symlink())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from ruamel.yaml import YAMLError
from vinca.configuration import read_vinca_yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_distro as importer


class ImportDistroTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.distros = self.root / "distros"
        self.distros.mkdir()
        (self.root / "tools").mkdir()
        (self.root / "tools" / "ci_default.yaml").write_text("platforms: [linux-64]\n")
        (self.shared / "patch").mkdir()
        (self.shared / "pinning").mkdir()
        (self.shared / "vinca.yaml").write_text("package_name_mode: new\npackages_skip: [shared_pkg]\n")
        (self.shared / "pkg_additional_info.yaml").write_text("demo:\n  additional_cmake_args: -DSHARED=ON\n")
        (self.shared / "patch" / "dependencies.yaml").write_text("{}\n")
        (self.shared / "pinning" / "overrides.yaml").write_text("pinning_overrides: {}\n")
        (self.source / "patch").mkdir()
        (self.source / "patch" / "ros-original-demo.linux.patch").write_bytes(b"original patch\n")
        (self.source / "patch" / "dependencies.yaml").write_text("demo:\n  add_run: [ros-original-support >=1]\n")
        (self.source / "vinca.yaml").write_text(
            "# source selection\nros_distro: original\npackage_name_mode: both\n"
            "packages_select_by_deps: [demo]\npackages_skip: [shared_pkg, local_pkg]\n"
        )
        (self.source / "rosdistro_snapshot.yaml").write_text(
            "demo:\n  version: 1.0.0\n  url: https://example.org/demo.git\n  tag: v1.0.0\n"
        )
        (self.source / "rosdistro_additional_recipes.yaml").write_text("extra:\n  version: 2.0.0\n")
        (self.source / "pkg_additional_info.yaml").write_text(
            "demo:\n  additional_cmake_args: -DSHARED=ON\n  build_number: 7\n"
        )
        (self.source / "pixi.toml").write_text('[tasks]\nupload = "rattler-build upload prefix -c robostack-original"\n')
        (self.source / "vinca_pinning.yaml").write_text(
            "conda_forge_pinning_version: 2026.10.01\nmigrations: [example]\n"
            "pinning_overrides:\n  python: ['3.13.*']\n"
        )
        for name, value in (("ROOT", self.root), ("DISTROS", self.distros), ("SHARED", self.shared)):
            context = patch.object(importer.rs, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.src = importer.Source(self.source, None)
        self.dest = self.distros / "renamed"

    def import_source(self):
        return importer.import_distro(self.src, "renamed")

    def seed_existing(self):
        self.import_source()
        (self.dest / "work" / "output").mkdir(parents=True)
        (self.dest / "work" / "output" / "valuable.conda").write_bytes(b"local build output\x00\xff")
        (self.dest / "notes.txt").write_bytes(b"unrelated local file\n")
        (self.dest / "tests").mkdir()
        (self.dest / "tests" / "custom.yaml").write_text("tests: [custom]\n")
        (self.dest / "conda_build_config.yaml").write_text("python: ['3.13']\n")
        (self.dest / "patch" / "local.patch").write_text("importer-owned local change\n")

    @staticmethod
    def tree(path):
        return {
            str(entry.relative_to(path)): (None if entry.is_dir() else entry.read_bytes())
            for entry in path.rglob("*")
        }

    def test_override_uses_source_identity_and_new_mode(self):
        self.assertEqual(self.import_source(), "renamed")
        config = read_vinca_yaml(self.dest / "vinca.yaml", "linux-64")
        self.assertEqual(config["ros_distro"], "renamed")
        self.assertEqual(config["package_name_mode"], "new")
        self.assertEqual(config["depmods"]["demo"]["add_run"], ["ros2-support >=1"])
        self.assertEqual(config["_snapshot"]["demo"]["version"], "1.0.0")
        self.assertEqual(config["_additional_packages_snapshot"]["extra"]["version"], "2.0.0")
        self.assertEqual(config["_pkg_additional_info"]["demo"]["build_number"], 7)
        self.assertEqual(config["packages_skip"], ["shared_pkg", "local_pkg"])
        self.assertEqual((self.dest / "patch" / "ros2-demo.linux.patch").read_bytes(), b"original patch\n")
        self.assertFalse((self.dest / "patch" / "ros-original-demo.linux.patch").exists())
        settings = yaml.safe_load((self.dest / "distro.yaml").read_text())
        self.assertEqual(settings["pinning_overrides"], {"python": ["3.13.*"]})
        self.assertEqual(settings["channel_name"], "robostack-original")

    def test_all_source_modes_cut_over_to_new(self):
        for mode in (None, "legacy", "both", "new"):
            with self.subTest(mode=mode):
                text = "ros_distro: original\n"
                if mode is not None:
                    text += f"package_name_mode: {mode}\n"
                (self.source / "vinca.yaml").write_text(text)
                self.import_source()
                self.assertEqual(read_vinca_yaml(self.dest / "vinca.yaml", "linux-64")["package_name_mode"], "new")

    def test_scalar_selector_preserves_both_platform_branches(self):
        (self.source / "vinca.yaml").write_text(
            "ros_distro: original\npackages_select_by_deps:\n"
            "  - if: linux\n    then: demo\n    else: portable_demo\n"
        )
        self.import_source()
        linux = read_vinca_yaml(self.dest / "vinca.yaml", "linux-64")
        windows = read_vinca_yaml(self.dest / "vinca.yaml", "win-64")
        self.assertEqual(linux["packages_select_by_deps"], ["demo"])
        self.assertEqual(windows["packages_select_by_deps"], ["portable_demo"])

    def test_nested_selector_preserves_list_and_empty_branches(self):
        (self.source / "vinca.yaml").write_text(
            "ros_distro: original\npackages_select_by_deps:\n"
            "  - if: unix\n    then:\n      - if: linux\n"
            "        then: [demo, linux_support]\n        else: mac_demo\n"
            "    else: null\n"
        )
        self.import_source()
        linux = read_vinca_yaml(self.dest / "vinca.yaml", "linux-64")
        macos = read_vinca_yaml(self.dest / "vinca.yaml", "osx-arm64")
        windows = read_vinca_yaml(self.dest / "vinca.yaml", "win-64")
        self.assertEqual(linux["packages_select_by_deps"], ["demo", "linux_support"])
        self.assertEqual(macos["packages_select_by_deps"], ["mac_demo"])
        self.assertEqual(windows["packages_select_by_deps"], [])

    def test_optional_deletions_preserve_unowned_files_without_copying(self):
        self.seed_existing()
        work_inode = (self.dest / "work").stat().st_ino
        local_files = {name: self.tree(self.dest)[name] for name in (
            "notes.txt", "tests/custom.yaml", "conda_build_config.yaml", "work/output/valuable.conda",
        )}
        for name in ("rosdistro_additional_recipes.yaml", "pkg_additional_info.yaml", "patch/dependencies.yaml"):
            (self.source / name).unlink()
        self.import_source()
        for name in ("rosdistro_additional_recipes.yaml", "pkg_additional_info.yaml", "patch/dependencies.yaml", "patch/local.patch"):
            self.assertFalse((self.dest / name).exists(), name)
        self.assertEqual((self.dest / "work").stat().st_ino, work_inode)
        for name, content in local_files.items():
            self.assertEqual((self.dest / name).read_bytes(), content)
        config = read_vinca_yaml(self.dest / "vinca.yaml", "linux-64")
        self.assertEqual(config["_additional_packages_snapshot"], {})
        self.assertEqual(config["depmods"], {})
        self.assertNotIn("build_number", config["_pkg_additional_info"]["demo"])

    def test_reimport_is_idempotent(self):
        self.seed_existing()
        self.import_source()
        before = self.tree(self.dest)
        self.import_source()
        self.assertEqual(self.tree(self.dest), before)
        self.assertEqual({path.name for path in self.distros.iterdir()}, {"renamed"})

    def test_missing_required_inputs_leave_destination_unchanged(self):
        self.seed_existing()
        before = self.tree(self.dest)
        for name in ("vinca.yaml", "rosdistro_snapshot.yaml", "patch"):
            with self.subTest(name=name):
                source_path = self.source / name
                saved = self.source / (name + ".saved")
                source_path.rename(saved)
                try:
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        self.import_source()
                    self.assertEqual(self.tree(self.dest), before)
                finally:
                    saved.rename(source_path)

    def test_invalid_inputs_leave_destination_unchanged(self):
        self.seed_existing()
        before = self.tree(self.dest)
        cases = (
            ("vinca.yaml", "[invalid"),
            ("vinca.yaml", "ros_distro: original\npackages_skip: bad\n"),
            ("vinca.yaml", "ros_distro: ../outside\n"),
            ("vinca.yaml", "ros_distro: original\npackages_select_by_deps:\n"
             "  - if: linux\n    then: [demo]\n    else: 42\n"),
            ("vinca.yaml", "ros_distro: original\npackages_select_by_deps:\n"
             "  - if: unix\n    then:\n      - if: linux\n        then: {invalid: demo}\n"),
            ("rosdistro_snapshot.yaml", ""),
            ("rosdistro_snapshot.yaml", "demo: invalid\n"),
            ("rosdistro_additional_recipes.yaml", "[invalid"),
            ("pkg_additional_info.yaml", "- not-a-mapping\n"),
            ("pkg_additional_info.yaml", "demo: invalid\n"),
            ("patch/dependencies.yaml", "[invalid"),
            ("patch/dependencies.yaml", "demo: invalid\n"),
            ("vinca_pinning.yaml", "[invalid"),
            ("vinca_pinning.yaml", "migrations: invalid\n"),
            ("pixi.toml", "invalid = ["),
        )
        for name, text in cases:
            with self.subTest(name=name, text=text):
                path = self.source / name
                old = path.read_bytes()
                path.write_text(text)
                try:
                    with self.assertRaises((ValueError, YAMLError)):
                        self.import_source()
                    self.assertEqual(self.tree(self.dest), before)
                finally:
                    path.write_bytes(old)

    def test_patch_copy_failure_leaves_existing_bytes_unchanged(self):
        self.seed_existing()
        before = self.tree(self.dest)
        original_copy = shutil.copytree

        def failed_copy(source, destination, **kwargs):
            original_copy(source, destination, **kwargs)
            raise OSError("copy failed after writing partial staged data")

        with patch.object(importer.shutil, "copytree", side_effect=failed_copy):
            with self.assertRaisesRegex(OSError, "copy failed"):
                self.import_source()
        self.assertEqual(self.tree(self.dest), before)

    def test_patch_name_collision_refuses_to_overwrite(self):
        self.seed_existing()
        before = self.tree(self.dest)
        (self.source / "patch" / "ros2-demo.linux.patch").write_bytes(b"different patch")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.import_source()
        self.assertEqual(self.tree(self.dest), before)

    def test_publish_failure_rolls_back_entire_destination(self):
        self.seed_existing()
        before = self.tree(self.dest)
        original_rename = Path.rename

        def failed_publish(path, target):
            if path.name == "new" and target == self.dest:
                raise PermissionError("destination is locked")
            return original_rename(path, target)

        with patch.object(Path, "rename", failed_publish):
            with self.assertRaisesRegex(PermissionError, "locked"):
                self.import_source()
        self.assertEqual(self.tree(self.dest), before)
        self.assertEqual({path.name for path in self.distros.iterdir()}, {"renamed"})

    def test_preservation_failure_rolls_back_moved_local_files(self):
        self.seed_existing()
        before = self.tree(self.dest)
        original_rename = Path.rename
        moved = 0

        def failed_preservation(path, target):
            nonlocal moved
            if path.parent.name == "previous" and path.name not in importer.OWNED_FILES:
                moved += 1
                if moved == 2:
                    raise PermissionError("local entry is locked")
            return original_rename(path, target)

        with patch.object(Path, "rename", failed_preservation):
            with self.assertRaisesRegex(PermissionError, "locked"):
                self.import_source()
        self.assertEqual(self.tree(self.dest), before)

    def test_failed_rollback_retains_recoverable_user_data(self):
        self.seed_existing()
        before = self.tree(self.dest)
        original_rename = Path.rename

        def failed_replacement_and_restore(path, target):
            if target == self.dest:
                raise PermissionError("destination remains locked")
            return original_rename(path, target)

        with patch.object(Path, "rename", failed_replacement_and_restore):
            with self.assertRaisesRegex(RuntimeError, "retained all recovery data"):
                self.import_source()
        transactions = list(self.distros.glob(".import-renamed-*"))
        self.assertEqual(len(transactions), 1)
        self.assertEqual(self.tree(transactions[0] / "previous"), before)

    def test_directory_in_owned_file_slot_is_not_deleted(self):
        self.seed_existing()
        (self.dest / "pkg_additional_info.yaml").unlink()
        (self.dest / "pkg_additional_info.yaml").mkdir()
        (self.dest / "pkg_additional_info.yaml" / "user-data").write_text("keep")
        before = self.tree(self.dest)
        with self.assertRaisesRegex(ValueError, "Refusing"):
            self.import_source()
        self.assertEqual(self.tree(self.dest), before)

    def test_invalid_destination_name_cannot_escape_distros(self):
        with self.assertRaisesRegex(ValueError, "Invalid ros_distro"):
            importer.import_distro(self.src, "../outside")
        self.assertFalse((self.root / "outside").exists())

    def make_git_source(self):
        subprocess.run(["git", "init", "-q", str(self.source)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True, capture_output=True)
        subprocess.run([
            "git", "-C", str(self.source), "-c", "user.name=Test", "-c", "user.email=test@example.org",
            "-c", "commit.gpgsign=false", "commit", "-qm", "fixture",
        ], check=True, capture_output=True)
        self.src = importer.Source(self.source, "HEAD")

    def test_git_ref_import_reads_committed_inputs(self):
        self.make_git_source()
        (self.source / "vinca.yaml").write_text("invalid working tree")
        self.import_source()
        config = read_vinca_yaml(self.dest / "vinca.yaml", "linux-64")
        self.assertEqual(config["ros_distro"], "renamed")
        self.assertEqual(config["depmods"]["demo"]["add_run"], ["ros2-support >=1"])

    def test_archive_failure_leaves_destination_unchanged(self):
        self.seed_existing()
        self.make_git_source()
        before = self.tree(self.dest)
        original_run = subprocess.run

        def failed_archive(command, **kwargs):
            if "archive" in command:
                raise subprocess.CalledProcessError(1, command, stderr=b"archive failed")
            return original_run(command, **kwargs)

        with patch.object(importer.subprocess, "run", side_effect=failed_archive):
            with self.assertRaises(subprocess.CalledProcessError):
                self.import_source()
        self.assertEqual(self.tree(self.dest), before)

    def test_corrupt_archive_leaves_destination_unchanged(self):
        self.seed_existing()
        self.make_git_source()
        before = self.tree(self.dest)
        original_run = subprocess.run

        def corrupt_archive(command, **kwargs):
            if "archive" in command:
                return subprocess.CompletedProcess(command, 0, stdout=b"not a tar archive")
            return original_run(command, **kwargs)

        with patch.object(importer.subprocess, "run", side_effect=corrupt_archive):
            with self.assertRaises(tarfile.ReadError):
                self.import_source()
        self.assertEqual(self.tree(self.dest), before)

    def test_git_errors_are_not_treated_as_missing_optional_inputs(self):
        self.seed_existing()
        self.make_git_source()
        before = self.tree(self.dest)
        original_run = subprocess.run

        def failed_optional_read(command, **kwargs):
            if "ls-tree" in command and command[-1] == "pkg_additional_info.yaml":
                raise subprocess.CalledProcessError(128, command, stderr=b"repository unavailable")
            return original_run(command, **kwargs)

        with patch.object(importer.subprocess, "run", side_effect=failed_optional_read):
            with self.assertRaises(subprocess.CalledProcessError):
                self.import_source()
        self.assertEqual(self.tree(self.dest), before)

    def test_default_destination_uses_source_distro(self):
        self.assertEqual(importer.import_distro(self.src, None), "original")
        config = read_vinca_yaml(self.distros / "original" / "vinca.yaml", "linux-64")
        self.assertEqual(config["ros_distro"], "original")
        self.assertEqual(config["package_name_mode"], "new")

    def test_failed_first_import_does_not_create_distribution(self):
        (self.source / "vinca_pinning.yaml").write_text("[invalid")
        with self.assertRaises(YAMLError):
            self.import_source()
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.distros.iterdir()), [])

    def test_git_ref_allows_absent_optional_files(self):
        for name in ("rosdistro_additional_recipes.yaml", "pkg_additional_info.yaml", "patch/dependencies.yaml"):
            (self.source / name).unlink()
        self.make_git_source()
        self.import_source()
        config = read_vinca_yaml(self.dest / "vinca.yaml", "linux-64")
        self.assertEqual(config["_additional_packages_snapshot"], {})
        self.assertEqual(config["depmods"], {})


if __name__ == "__main__":
    unittest.main()

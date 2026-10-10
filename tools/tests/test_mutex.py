"""The distro mutex keeps one ROS distribution per environment and nothing else:
library versions come from the packages' own dependencies."""

import pytest

import maintenance
import robostack as rs


def test_mutex_without_constraints_is_fine():
    assert maintenance.mutex_problems({"mutex_package": {"name": "ros2-distro-mutex", "run_constraints": []}}) == []
    assert maintenance.mutex_problems({"mutex_package": {"name": "ros2-distro-mutex"}}) == []
    assert maintenance.mutex_problems({}) == []


def test_mutex_constraints_are_rejected():
    problems = maintenance.mutex_problems({"mutex_package": {"run_constraints": ["libprotobuf 7.35.*"]}})
    assert len(problems) == 1
    assert "run_constraints must stay empty" in problems[0]


@pytest.mark.parametrize("distro", rs.distros())
def test_no_distribution_constrains_its_mutex(distro):
    # shared/vinca.yaml and the distribution's vinca.yaml as vinca combines them
    vinca = rs.read_vinca(distro, "linux-64")
    assert maintenance.mutex_problems(vinca) == []


def test_partial_rebuild_leaves_the_mutex_alone(tmp_path, monkeypatch):
    distro = tmp_path / "distros" / "testdistro"
    distro.mkdir(parents=True)
    vinca = "ros_distro: testdistro\nbuild_number: 5\n\nmutex_package:\n  version: \"0.3.0\"\n"
    (distro / "vinca.yaml").write_text(vinca)
    (distro / "pkg_additional_info.yaml").write_text("rclcpp:\n  build_number: 3\n  add_host: [foo]\n")
    monkeypatch.setattr(rs, "DISTROS", tmp_path / "distros")
    monkeypatch.setattr(maintenance, "released_builds", lambda d: {
        "ros2-rclcpp": 7, "ros-testdistro-std-msgs": 6, "ros2-distro-mutex": 40, "libfoo": 99})
    monkeypatch.setattr(rs, "read_vinca", lambda d, platform=None: {"mutex_package": {"name": "ros2-distro-mutex"}})

    lines = maintenance.bump_partial("testdistro", {"rclcpp": "uses libboost", "std_msgs": "depends on rclcpp"})

    # above every released ROS package (7), ignoring the mutex and non-ROS packages
    assert lines == ["2 packages get `build_number: 8` in `distros/testdistro/pkg_additional_info.yaml`"]
    info = maintenance.yaml.safe_load((distro / "pkg_additional_info.yaml").read_text())
    assert info == {"rclcpp": {"build_number": 8, "add_host": ["foo"]}, "std_msgs": {"build_number": 8}}
    assert (distro / "vinca.yaml").read_text() == vinca


def test_partial_rebuild_keeps_pkg_additional_info_sorted(tmp_path, monkeypatch):
    from vinca.sort_yaml_keys import sort_mapping_keys

    distro = tmp_path / "distros" / "testdistro"
    distro.mkdir(parents=True)
    (distro / "vinca.yaml").write_text("ros_distro: testdistro\nbuild_number: 5\n")
    (distro / "pkg_additional_info.yaml").write_text("zeta:\n  add_host: [foo]\nalpha:\n  build_number: 3\n")
    monkeypatch.setattr(rs, "DISTROS", tmp_path / "distros")
    monkeypatch.setattr(maintenance, "released_builds", lambda d: {"ros2-alpha": 7})
    monkeypatch.setattr(rs, "read_vinca", lambda d, platform=None: {"mutex_package": {"name": "ros2-distro-mutex"}})

    maintenance.bump_partial("testdistro", {"mid": "uses libboost", "beta": "uses libboost"})

    info = distro / "pkg_additional_info.yaml"
    assert sort_mapping_keys(info) is False  # already in `pixi run sort` order: nothing to change

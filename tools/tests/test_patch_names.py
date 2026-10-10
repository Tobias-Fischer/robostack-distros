"""vinca applies a patch only when its file name names a package of the distribution."""

import maintenance


def test_unmatched_patches():
    packages = {"rosidl_cli", "rmw_implementation_cmake"}
    patches = [
        "ros2-rosidl-cli.patch",
        "ros2-rmw-implementation-cmake.osx.patch",
        "ros2-rosidl.patch",  # names a package that doesn't exist
        "ros2-rwm-implementation-cmake.osx.patch",  # typo
        "README.patch",  # misnamed, reported by the naming check instead
    ]
    assert maintenance.unmatched_patches(patches, packages) == [
        "ros2-rosidl.patch",
        "ros2-rwm-implementation-cmake.osx.patch",
    ]

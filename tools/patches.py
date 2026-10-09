"""Read effective source patches through the pinned Vinca resolver."""

from pathlib import Path
from typing import Any, Iterator

from vinca.configuration import read_vinca_yaml
from vinca.sources import package_patches

# Check both supported architectures: selectors can change a layer's patch_dir.
TARGET_PLATFORMS = (
    "linux-64",
    "linux-aarch64",
    "osx-64",
    "osx-arm64",
    "win-64",
    "emscripten-wasm32",
)


def resolved_patch_configs(vinca: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Keep Vinca's layer overrides, aliases and selectors authoritative."""
    for platform in TARGET_PLATFORMS:
        yield platform, read_vinca_yaml(vinca, target_platform=platform)


def configured_patches(config: dict[str, Any], platform: str) -> dict[str, list[Path]]:
    """Return the patches each package would actually receive on this target."""
    return {
        name: [Path(path) for path in package_patches(name, config, platform)]
        for name in config["_patches"]
    }

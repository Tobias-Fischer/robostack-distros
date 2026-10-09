"""Namespace PR build progress by its inputs, including ci.yaml rebuild controls.

An epoch includes shared inputs, the selected distribution, generated recipes and
pins. A retry restores only that epoch; changing full_rebuild/evict_cache, patches,
recipes, pins or tooling starts a fresh cache. Replacements are never evicted on
subsequent attempts with the same inputs.
"""

import argparse
import hashlib
import shutil
import subprocess
from pathlib import Path

from release import relevant_path


ROOT = Path(__file__).resolve().parents[1]
MARKER = ".robostack-cache-epoch"


def cache_epoch(root: Path, distro: str, platform: str) -> str:
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                             stdout=subprocess.PIPE).stdout.decode().split("\0")
    inputs = {name for name in tracked if name and relevant_path(name, distro)}
    work = root / "distros" / distro / "work"
    # The generator's result is also an input: do not reuse output if upstream
    # recipe generation changes even while the checked-in inputs stay identical.
    for directory in (work / "recipes", work / "recipes_only_patch"):
        if directory.is_dir():
            inputs.update(path.relative_to(root).as_posix() for path in directory.rglob("*")
                          if path.is_file())
    for name in ("vinca.yaml", "vinca_pinning.yaml", "conda_build_config.yaml"):
        path = work / name
        if path.is_file():
            inputs.add(path.relative_to(root).as_posix())
    digest = hashlib.sha256(f"robostack-cache-v2\0{distro}\0{platform}\0".encode())
    # Resetting and later re-enabling the same controls must not resurrect a
    # previous rebuild's cache. Retries of this control revision stay identical.
    control_revision = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", f"distros/{distro}/ci.yaml"],
        cwd=root, check=True, stdout=subprocess.PIPE,
    ).stdout.strip()
    digest.update(control_revision + b"\0")
    for name in sorted(inputs):
        path = root / name
        digest.update(name.encode() + b"\0")
        if path.is_file():
            digest.update(b"file\0" + hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"deleted\0")
    return digest.hexdigest()


def prepare_cache(cache: Path, epoch: str) -> None:
    """Reject foreign/unmarked progress once, then preserve this epoch's outputs."""
    marker = cache / MARKER
    if marker.is_file() and marker.read_text().strip() == epoch:
        return
    if cache.exists():
        for child in cache.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    cache.mkdir(parents=True, exist_ok=True)
    marker.write_text(epoch + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--distro", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    epoch = cache_epoch(ROOT, args.distro, args.platform)
    if args.cache_dir is not None:
        prepare_cache(args.cache_dir, epoch)
    else:
        print(epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

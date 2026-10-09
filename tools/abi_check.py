"""Compare the ABI of two builds of RoboStack packages with libabigail's abidiff.

    python tools/abi_check.py --channel https://conda.anaconda.org/robostack-jazzy --latest-pairs 40
    python tools/abi_check.py --old a.conda --new b.conda

For every shared library (lib/*.so*) both builds contain, abidiff compares the
two. Without debug information (the published packages are stripped) only the
exported ELF symbols are compared: removed or changed functions and variables
show up, changed struct layouts don't.

Verdicts per package (the worst of its libraries):
- compatible: no ABI change;
- additions: only added symbols (dependents keep working);
- soname: the library's SONAME changed, but no symbol was removed or changed.
  Dependents are linked to the old SONAME, so they must be rebuilt (relinked),
  even though nothing about the API changed (e.g. MoveIt's versioned SONAMEs);
- incompatible: removed or changed symbols, or a removed library (dependents
  must be rebuilt);
- no libraries: nothing to compare (Python, messages without C++ libs, data).

Linux only: abidiff reads ELF. It is run with `pixi exec -s libabigail`.
"""

from __future__ import annotations

import argparse
import io
import re
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import zstandard

# abidiff exit status bits (libabigail's abidiff_status)
ABI_ERROR = 1
ABI_USAGE_ERROR = 2
ABI_CHANGE = 4
ABI_INCOMPATIBLE_CHANGE = 8


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "robostack-abi-check"}), timeout=300)


def extract_conda(path: Path, dest: Path) -> None:
    """Unpack a .conda (zip with zstd-compressed tarballs) or .tar.bz2 package."""
    dest.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".conda":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.startswith("pkg-") and name.endswith(".tar.zst"):
                    data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(z.read(name)))
                    with tarfile.open(fileobj=data, mode="r|") as tar:
                        tar.extractall(dest, filter="data")
    else:
        with tarfile.open(path, "r:bz2") as tar:
            tar.extractall(dest, filter="data")


def libraries(prefix: Path) -> dict[str, Path]:
    """Shared libraries under lib/, keyed by their name without version suffixes."""
    libs = {}
    for f in (prefix / "lib").glob("**/*.so*") if (prefix / "lib").is_dir() else []:
        if f.is_file() and not f.is_symlink():
            libs[f.name.split(".so")[0] + ".so"] = f
    return libs


def abidiff(old: Path, new: Path) -> tuple[int, str]:
    cmd = ["pixi", "exec", "-s", "libabigail", "abidiff", "--no-show-locs", "--no-unreferenced-symbols",
           str(old), str(new)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


RANK = ["compatible", "additions", "soname", "error", "incompatible"]
_SUMMARY_RE = re.compile(r"(Functions|Variables) changes summary: (\d+) Removed, (\d+) Changed")


def classify(rc: int, report: str) -> str:
    """Verdict for one library from abidiff's exit status and report."""
    if rc & (ABI_ERROR | ABI_USAGE_ERROR):
        return "error"
    if not rc & (ABI_CHANGE | ABI_INCOMPATIBLE_CHANGE):
        return "compatible"
    removed_or_changed = any(int(m.group(2)) or int(m.group(3)) for m in _SUMMARY_RE.finditer(report))
    if rc & ABI_INCOMPATIBLE_CHANGE and not removed_or_changed and "SONAME changed" in report:
        return "soname"
    if rc & ABI_INCOMPATIBLE_CHANGE:
        return "incompatible"
    return "additions"


def compare(old_pkg: Path, new_pkg: Path, work: Path) -> dict:
    a, b = work / "old", work / "new"
    shutil.rmtree(work, ignore_errors=True)
    extract_conda(old_pkg, a)
    extract_conda(new_pkg, b)
    old_libs, new_libs = libraries(a), libraries(b)
    result = {"libraries": {}, "removed_libraries": sorted(set(old_libs) - set(new_libs))}
    if not old_libs and not new_libs:
        result["verdict"] = "no libraries"
        return result
    verdict = "incompatible" if result["removed_libraries"] else "compatible"
    for name in sorted(set(old_libs) & set(new_libs)):
        rc, text = abidiff(old_libs[name], new_libs[name])
        lib_verdict = classify(rc, text)
        result["libraries"][name] = {"verdict": lib_verdict, "report": "\n".join(text.splitlines()[:40])}
        if RANK.index(lib_verdict) > RANK.index(verdict):
            verdict = lib_verdict
    result["verdict"] = verdict
    return result


def _is_compatibility_package(rec: dict) -> bool:
    """vinca's ros-<distro>-<pkg> packages (package_name_mode: both) are empty and
    only depend on ros2-<pkg> of the same version."""
    deps = rec.get("depends", [])
    return len(deps) == 1 and deps[0].startswith("ros2-") and "==" in deps[0]


def latest_pairs(channel: str, platform: str, count: int, distro: str) -> list[tuple[str, dict, dict]]:
    """Packages whose two newest versions on the channel differ, newest real build of
    each; ros-<distro>-<pkg> and ros2-<pkg> count as the same package."""
    with _open(f"{channel.rstrip('/')}/{platform}/repodata.json") as r:
        data = json.load(r)
    records = list(data.get("packages", {}).items()) + list(data.get("packages.conda", {}).items())
    prefixes = (f"ros-{distro}-", "ros2-")
    by_name: dict[str, dict[str, tuple[str, dict]]] = {}
    for filename, rec in records:
        prefix = next((p for p in prefixes if rec["name"].startswith(p)), None)
        if prefix is None or _is_compatibility_package(rec):
            continue
        short = rec["name"][len(prefix):]
        versions = by_name.setdefault(short, {})
        current = versions.get(rec["version"])
        if current is None or (rec.get("build_number", 0), rec.get("timestamp", 0)) > (
            current[1].get("build_number", 0), current[1].get("timestamp", 0)
        ):
            versions[rec["version"]] = (filename, rec)

    def key(v: str):
        return [int(p) if p.isdigit() else 0 for p in v.replace("-", ".").split(".")]

    pairs = []
    for short, versions in sorted(by_name.items()):
        if len(versions) < 2:
            continue
        ordered = sorted(versions, key=key)
        old, new = versions[ordered[-2]], versions[ordered[-1]]
        pairs.append((short, {"file": old[0], **old[1]}, {"file": new[0], **new[1]}))
    # most recently uploaded new versions first
    pairs.sort(key=lambda p: p[2].get("timestamp", 0), reverse=True)
    return pairs[:count]


def download(channel: str, platform: str, filename: str, dest: Path) -> Path:
    target = dest / filename
    if not target.exists():
        with _open(f"{channel.rstrip('/')}/{platform}/{filename}") as r, open(target, "wb") as f:
            shutil.copyfileobj(r, f)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", help="channel URL to take version pairs from")
    parser.add_argument("--platform", default="linux-64")
    parser.add_argument("--distro", default="jazzy", help="for the ros-<distro>-* package names")
    parser.add_argument("--latest-pairs", type=int, default=30)
    parser.add_argument("--old", type=Path)
    parser.add_argument("--new", type=Path)
    parser.add_argument("--json", type=Path, help="write the results here")
    parser.add_argument("--summary", type=Path, help="append a Markdown summary here")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("abidiff reads ELF: run this on Linux")

    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if args.old and args.new:
            results[args.new.name] = compare(args.old, args.new, tmp / "work")
        else:
            downloads = tmp / "downloads"
            downloads.mkdir()
            for name, old, new in latest_pairs(args.channel, args.platform, args.latest_pairs, args.distro):
                print(f"{name}: {old['version']} -> {new['version']}", flush=True)
                old_pkg = download(args.channel, args.platform, old["file"], downloads)
                new_pkg = download(args.channel, args.platform, new["file"], downloads)
                res = compare(old_pkg, new_pkg, tmp / "work")
                res.update(old=old["version"], new=new["version"])
                results[name] = res
                print(f"  -> {res['verdict']}", flush=True)
                old_pkg.unlink(missing_ok=True)
                new_pkg.unlink(missing_ok=True)

    counts: dict[str, int] = {}
    for res in results.values():
        counts[res["verdict"]] = counts.get(res["verdict"], 0) + 1
    print("\nSummary: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
    if args.summary:
        lines = ["### ABI check", "", "| package | old → new | verdict | libraries |", "|---|---|---|---|"]
        for name, res in results.items():
            libs = ", ".join(f"{lib} ({info['verdict']})" for lib, info in res["libraries"].items())
            if res["removed_libraries"]:
                libs += " removed: " + ", ".join(res["removed_libraries"])
            lines.append(f"| {name} | {res.get('old', '')} → {res.get('new', '')} | {res['verdict']} | {libs} |")
        lines += ["", "Summary: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))]
        with open(args.summary, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Compare the ABI of two builds of RoboStack packages with libabigail's abidiff.

    python tools/abi_check.py --distro jazzy --latest-pairs 40
    python tools/abi_check.py --old a.conda --new b.conda
    python tools/abi_check.py --distro jazzy --pr-builds distros/jazzy/work/output/linux-64

--pr-builds compares every package built in a pull request with the newest build of
the same package on the distribution's channel, and lists the released packages that
depend on it: the ones to rebuild too when the verdict is soname or incompatible.

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


def read_index(path: Path) -> dict:
    """info/index.json of a package."""
    if path.suffix == ".conda":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.startswith("info-") and n.endswith(".tar.zst"))
            data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(z.read(name)))
            with tarfile.open(fileobj=data, mode="r|") as tar:
                for member in tar:
                    if member.name == "info/index.json":
                        return json.load(tar.extractfile(member))
    else:
        with tarfile.open(path, "r:bz2") as tar:
            return json.load(tar.extractfile("info/index.json"))
    raise ValueError(f"{path} has no info/index.json")


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


def short_name(name: str, distro: str) -> str | None:
    """The ROS package of a conda name: ros-<distro>-<pkg> and ros2-<pkg> are the same."""
    for prefix in (f"ros-{distro}-", "ros2-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def _version_key(v: str):
    return [int(p) if p.isdigit() else 0 for p in v.replace("-", ".").split(".")]


def released(channel: str, platform: str, distro: str) -> dict[str, dict[str, dict]]:
    """The channel's ROS packages: package -> version -> newest real build (with its
    "file"); vinca's empty compatibility packages are left out."""
    with _open(f"{channel.rstrip('/')}/{platform}/repodata.json") as r:
        data = json.load(r)
    records = list(data.get("packages", {}).items()) + list(data.get("packages.conda", {}).items())
    by_name: dict[str, dict[str, dict]] = {}
    for filename, rec in records:
        short = short_name(rec["name"], distro)
        if short is None or _is_compatibility_package(rec):
            continue
        versions = by_name.setdefault(short, {})
        current = versions.get(rec["version"])
        if current is None or (rec.get("build_number", 0), rec.get("timestamp", 0)) > (
            current.get("build_number", 0), current.get("timestamp", 0)
        ):
            versions[rec["version"]] = {"file": filename, **rec}
    return by_name


def newest(versions: dict[str, dict]) -> dict:
    return versions[max(versions, key=_version_key)]


def latest_pairs(channel: str, platform: str, count: int, distro: str) -> list[tuple[str, dict, dict]]:
    """Packages whose two newest versions on the channel differ, newest real build of
    each; ros-<distro>-<pkg> and ros2-<pkg> count as the same package."""
    pairs = []
    for short, versions in sorted(released(channel, platform, distro).items()):
        if len(versions) < 2:
            continue
        ordered = sorted(versions, key=_version_key)
        pairs.append((short, versions[ordered[-2]], versions[ordered[-1]]))
    # most recently uploaded new versions first
    pairs.sort(key=lambda p: p[2].get("timestamp", 0), reverse=True)
    return pairs[:count]


def dependents(by_name: dict[str, dict[str, dict]], package: str, distro: str) -> list[str]:
    """Released packages (newest version of each) that depend on the package directly."""
    found = []
    for short, versions in by_name.items():
        if short == package:
            continue
        if any(short_name(dep.split()[0], distro) == package for dep in newest(versions).get("depends", [])):
            found.append(short)
    return sorted(found)


RUNTIMES = {"libgcc", "libgcc-ng", "libstdcxx", "libstdcxx-ng", "libcxx", "vc", "vc14_runtime", "vcomp14", "ucrt"}


def pin_changes(old: dict, new: dict, distro: str) -> list[str]:
    """Non-ROS dependencies whose pin differs between the two builds, e.g. a changed
    libboost: then the ABI comparison mixes the package's change with the pin's."""
    def pins(rec):
        out = {}
        for dep in rec.get("depends", []):
            name, *spec = dep.split(" ", 1)
            # the distro mutex counts as ROS; compiler runtimes and virtual packages are noise
            if short_name(name, distro) is None and not name.startswith("__") and name not in RUNTIMES:
                out[name] = spec[0] if spec else ""
        return out

    a, b = pins(old), pins(new)
    return [f"{n} {a.get(n, '(none)')} → {b.get(n, '(none)')}" for n in sorted(set(a) | set(b)) if a.get(n) != b.get(n)]


def channel_for(distro: str) -> str:
    """The distribution's release channel (repo.prefix.dev serves repodata directly)."""
    sys.path.insert(0, str(Path(__file__).parent))
    import robostack as rs

    s = rs.settings(distro)
    name = s.get("channel_name", f"robostack-{distro}")
    if s.get("upload_target", "prefix") == "prefix":
        return f"https://repo.prefix.dev/{name}"
    return f"https://conda.anaconda.org/{name}"


def download(channel: str, platform: str, filename: str, dest: Path) -> Path:
    target = dest / filename
    if not target.exists():
        with _open(f"{channel.rstrip('/')}/{platform}/{filename}") as r, open(target, "wb") as f:
            shutil.copyfileobj(r, f)
    return target


def check_pr_builds(pr_dir: Path, channel: str, platform: str, distro: str, tmp: Path) -> dict:
    """Compare every package in pr_dir with the newest released build of it."""
    by_name = released(channel, platform, distro)
    downloads = tmp / "downloads"
    downloads.mkdir(exist_ok=True)
    results = {}
    for pkg in sorted([*pr_dir.glob("*.conda"), *pr_dir.glob("*.tar.bz2")]):
        index = read_index(pkg)
        short = short_name(index["name"], distro)
        if short is None or _is_compatibility_package(index):
            continue
        if short not in by_name:
            results[short] = {"verdict": "new package", "new": index["version"], "libraries": {}, "removed_libraries": []}
            continue
        old = newest(by_name[short])
        if old["file"] == pkg.name:
            continue
        print(f"{short}: {old['version']} (build {old.get('build_number', 0)}) -> {index['version']} (PR)", flush=True)
        old_pkg = download(channel, platform, old["file"], downloads)
        res = compare(old_pkg, pkg, tmp / "work")
        old_pkg.unlink(missing_ok=True)
        res.update(old=f"{old['version']} (build {old.get('build_number', 0)})", new=index["version"],
                   pins=pin_changes(old, index, distro))
        if res["verdict"] in ("soname", "incompatible"):
            res["dependents"] = dependents(by_name, short, distro)
        results[short] = res
        print(f"  -> {res['verdict']}", flush=True)
    return results


VERDICT_NOTE = {
    "compatible": "no ABI change",
    "additions": "only added symbols",
    "soname": "SONAME changed: rebuild the dependents",
    "incompatible": "removed or changed symbols: rebuild the dependents",
    "error": "abidiff failed",
    "no libraries": "nothing to compare",
    "new package": "not released yet",
}


def pr_summary(results: dict, distro: str, platform: str, channel: str) -> list[str]:
    lines = [f"#### {distro} {platform}", ""]
    if not results:
        return lines + ["No package of this pull request has a released build to compare with.", ""]
    lines += ["| package | released → PR | verdict | details |", "|---|---|---|---|"]
    def worst_first(item):
        verdict = item[1]["verdict"]
        return (-RANK.index(verdict) if verdict in RANK else 1, item[0])

    for name, res in sorted(results.items(), key=worst_first):
        details = []
        changed = [f"`{lib}` ({info['verdict']})" for lib, info in res["libraries"].items() if info["verdict"] != "compatible"]
        if changed:
            details.append("libraries: " + ", ".join(changed))
        if res["removed_libraries"]:
            details.append("removed: " + ", ".join(f"`{lib}`" for lib in res["removed_libraries"]))
        if res.get("dependents") is not None:
            deps = res["dependents"]
            details.append(f"**{len(deps)} released packages depend on it** (rebuild those with compiled code): "
                           + (", ".join(deps) if deps else "none"))
        if res.get("pins"):
            details.append("⚠️ changed pins (the comparison includes them): " + ", ".join(res["pins"]))
        verdict = f"{res['verdict']}: {VERDICT_NOTE.get(res['verdict'], '')}"
        lines.append(f"| {name} | {res.get('old', '')} → {res.get('new', '')} | {verdict} | {'<br>'.join(details)} |")
    return lines + ["", f"Compared with the newest builds on {channel}/{platform}.", ""]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", help="channel URL (default: the distribution's release channel)")
    parser.add_argument("--platform", default="linux-64")
    parser.add_argument("--distro", default="jazzy", help="for the ros-<distro>-* package names")
    parser.add_argument("--latest-pairs", type=int, default=30)
    parser.add_argument("--old", type=Path)
    parser.add_argument("--new", type=Path)
    parser.add_argument("--pr-builds", type=Path, help="directory with the packages a pull request built")
    parser.add_argument("--json", type=Path, help="write the results here")
    parser.add_argument("--summary", type=Path, help="append a Markdown summary here")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("abidiff reads ELF: run this on Linux")
    channel = args.channel or channel_for(args.distro)

    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if args.pr_builds:
            results = check_pr_builds(args.pr_builds, channel, args.platform, args.distro, tmp) if args.pr_builds.is_dir() else {}
        elif args.old and args.new:
            results[args.new.name] = compare(args.old, args.new, tmp / "work")
        else:
            downloads = tmp / "downloads"
            downloads.mkdir()
            for name, old, new in latest_pairs(channel, args.platform, args.latest_pairs, args.distro):
                print(f"{name}: {old['version']} -> {new['version']}", flush=True)
                old_pkg = download(channel, args.platform, old["file"], downloads)
                new_pkg = download(channel, args.platform, new["file"], downloads)
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
        if args.pr_builds:
            lines = pr_summary(results, args.distro, args.platform, channel)
        else:
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

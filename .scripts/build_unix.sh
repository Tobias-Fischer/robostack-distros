#!/usr/bin/env bash


# Default values
target=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --target) target="$2"; shift ;;  # Capture value for --target
        *) echo "Unknown parameter passed: $1"; exit 1 ;;  # Handle invalid options
    esac
    shift
done

set -xeuo pipefail
export PYTHONUNBUFFERED=1

# ROBOSTACK_DISTRO is set by the generated build workflow (tools/robostack.py gha).
: "${ROBOSTACK_DISTRO:?ROBOSTACK_DISTRO is not set}"
cd "distros/${ROBOSTACK_DISTRO}/work"
export FEEDSTOCK_ROOT=`pwd`
export "CONDA_BLD_PATH=$HOME/conda-bld/"

# git's background auto-maintenance can hold (and then delete) a lock file in a
# cached source clone while rattler-build copies it, failing the build with
# "FileSystem error: ... .git/objects/maintenance.lock does not exist".
git config --global maintenance.auto false

curl -fsSL https://pixi.sh/install.sh | bash
export PATH="$HOME/.pixi/bin:$PATH"

if [[ "$target" == *"osx"* ]]; then
    echo "osx"
    export PATH=$(echo $PATH | tr ":" "\n" | grep -v 'homebrew' | xargs | tr ' ' ':')
fi

if [[ "$target" == "emscripten-wasm32" ]]; then
    extra_channel="-c https://repo.mamba.pm/emscripten-forge"
    cross_compile="--target-platform emscripten-wasm32 --test skip"

else
    extra_channel=""
    cross_compile=""
fi


for recipe in ${CURRENT_RECIPES[@]}; do
	# build-ci (tools/robostack.py) adds the variant config and the distro's channels.
	pixi run -v rs "${ROBOSTACK_DISTRO}" build-ci \
		--recipe ${FEEDSTOCK_ROOT}/recipes/${recipe} \
		${extra_channel} \
		--output-dir $CONDA_BLD_PATH \
		${cross_compile}
		# -m ${FEEDSTOCK_ROOT}/.ci_support/conda_forge_pinnings.yaml \

done

# Check if it build something, this is a hotfix for the skips inside additional_recipes
shopt -s nullglob
conda_packages=( "${CONDA_BLD_PATH}/${target}"*/*.conda )
if (( ${#conda_packages[@]} > 0 )); then
    # Upload packages one-by-one; the upload task (tools/robostack.py) skips or overwrites
    # packages that already exist, depending on the upload target.
    for conda_package in "${conda_packages[@]}"; do
        pixi run rs "${ROBOSTACK_DISTRO}" upload "${conda_package}"
    done
else
    echo "Warning: No .conda files found in ${CONDA_BLD_PATH}/${target}"
    echo "This might be due to all the packages being skipped"
fi

#!/usr/bin/env bash
# Run from any directory: linux-smoke.sh /path/to/blender /path/to/qualified-extension.zip
set -euo pipefail

usage() {
    printf 'Usage: %s /path/to/blender /path/to/qualified-hard-surface-workflow.zip\n' "${0##*/}"
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi
if [[ $# != 2 ]]; then
    usage >&2
    exit 2
fi
if [[ $(uname -s) != Linux ]]; then
    printf 'This smoke test requires Linux.\n' >&2
    exit 2
fi
if [[ ! -f $1 || ! -x $1 ]]; then
    printf 'Blender executable not found: %s\n' "$1" >&2
    exit 2
fi
if [[ ! -f $2 || ! -r $2 ]]; then
    printf 'Extension ZIP not readable: %s\n' "$2" >&2
    exit 2
fi

blender=$(realpath -- "$1")
archive=$(realpath -- "$2")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

# Higher-precedence user overrides would defeat BLENDER_USER_RESOURCES.
unset BLENDER_USER_CONFIG BLENDER_USER_SCRIPTS BLENDER_USER_DATAFILES BLENDER_USER_EXTENSIONS
export BLENDER_USER_RESOURCES="$work/profile"
export XDG_CACHE_HOME="$work/cache"
mkdir -p -- "$BLENDER_USER_RESOURCES" "$XDG_CACHE_HOME"

printf '\n[1/3] Installing and enabling extension in an isolated profile\n'
"$blender" --background -noaudio --offline-mode --command extension install-file --repo user_default --enable "$archive"
# Blender can exit zero even if archive extraction failed.
if [[ ! -f "$BLENDER_USER_RESOURCES/extensions/user_default/hard_surface_workflow/blender_manifest.toml" ]]; then
    printf 'Hard Surface Workflow was not installed in the isolated user_default repository.\n' >&2
    exit 1
fi

printf '\n[2/3] Cold start: checking registration and running Split Face\n'
"$blender" --background -noaudio --offline-mode --disable-autoexec --python-exit-code 1 \
    --python "$script_dir/linux_smoke.py" -- split "$work/split-face.blend"

printf '\n[3/3] Cold reopen: checking saved topology\n'
"$blender" --background -noaudio --offline-mode --disable-autoexec "$work/split-face.blend" --python-exit-code 1 \
    --python "$script_dir/linux_smoke.py" -- reopen "$work/split-face.blend"

printf '\nPASS: Linux headless install, cold-start registration, Split Face, and saved topology.\n'

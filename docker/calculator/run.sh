#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
image_name=idp-calculator-e2e
default_token_file=$HOME/Documents/ORCA/Halliburton/copilot-docker.token
token_file=${1:-$default_token_file}

if [ $# -gt 0 ]; then
    shift
fi

if [ ! -f "$token_file" ]; then
    printf 'token file not found: %s\n' "$token_file" >&2
    exit 1
fi

out_dir=$repo_root/docker/calculator/out
mkdir -p "$out_dir"

env_file=$(mktemp "${TMPDIR:-/tmp}/copilot-docker.XXXXXX")
cleanup() {
    rm -f "$env_file"
}
trap cleanup EXIT INT TERM HUP

token=$(tr -d '\r\n' < "$token_file")
if [ -z "$token" ]; then
    printf 'token file is empty: %s\n' "$token_file" >&2
    exit 1
fi
printf 'COPILOT_GITHUB_TOKEN=%s\n' "$token" > "$env_file"
unset token

docker build --file "$repo_root/docker/calculator/Dockerfile" --tag "$image_name" "$repo_root"
docker run --rm \
    --env-file "$env_file" \
    --volume "$out_dir:/work/platform/docker/calculator/out" \
    "$image_name" \
    python3 docker/calculator/scenario.py "$@"

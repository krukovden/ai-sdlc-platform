#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
image_name=idp-calculator-e2e
container_name=idp-ado-test
default_token_file=$HOME/Documents/ORCA/Halliburton/copilot-docker.token
mode=fake

if [ "${1:-}" = "ado" ]; then
    mode=ado
    shift
elif [ "${1:-}" = "fake" ]; then
    shift
fi

token_file=${1:-$default_token_file}

if [ "$mode" = "fake" ] && [ $# -gt 0 ]; then
    shift
fi

if [ ! -f "$token_file" ]; then
    printf 'token file not found: %s\n' "$token_file" >&2
    exit 1
fi

out_dir=$repo_root/docker/calculator/out
mkdir -p "$out_dir"

env_file=$(mktemp "${TMPDIR:-/tmp}/copilot-docker.XXXXXX")
run_status=0

stop_container() {
    if ! docker ps -a --format '{{.Names}}' | grep -qx "$container_name"; then
        return 0
    fi
    docker stop "$container_name" >/dev/null
    running=$(docker inspect -f '{{.State.Running}}' "$container_name")
    if [ "$running" != "false" ]; then
        printf 'container %s is still running after docker stop\n' "$container_name" >&2
        return 1
    fi
}

cleanup() {
    status=$?
    rm -f "$env_file"
    if [ "$mode" = "ado" ]; then
        if ! stop_container; then
            status=1
        fi
    fi
    exit "$status"
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

if [ "$mode" = "ado" ]; then
    if [ -z "${AZURE_DEVOPS_EXT_PAT:-}" ]; then
        printf 'AZURE_DEVOPS_EXT_PAT is not set. Source ~/.halliburton/ado-env.sh first.\n' >&2
        exit 1
    fi
    if docker ps -a --format '{{.Names}}' | grep -qx "$container_name"; then
        docker start -ai "$container_name"
    else
        docker run \
            --name "$container_name" \
            --env-file "$env_file" \
            --env AZURE_DEVOPS_EXT_PAT \
            --volume "$out_dir:/work/platform/docker/calculator/out" \
            "$image_name" \
            python3 docker/calculator/scenario.py \
            --board ado \
            --org "https://dev.azure.com/HAL-TSS" \
            --project "TSS-Orion" \
            --i-understand-this-writes
    fi
    printf 'container left in place: docker start -ai %s\n' "$container_name"
else
    docker run --rm \
        --env-file "$env_file" \
        --volume "$out_dir:/work/platform/docker/calculator/out" \
        "$image_name" \
        python3 docker/calculator/scenario.py "$@"
fi
